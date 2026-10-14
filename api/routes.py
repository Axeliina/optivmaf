import asyncio
import json
import os
import shutil
import time
import threading

from pathlib import Path
from typing import List, Optional, Union
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel

from core.database import get_db, VideoRecord, JobStatus, SessionLocal
from core.config import load_config
from core.stats import get_io_stats, reset_io_stats, add_io_stats
from core.sysmon import get_system_stats
from core.nodes import register_or_update_node, get_all_nodes, get_node, update_node_job, record_node_completed
from workers.scanner import scan_library

router = APIRouter()

CONFIG_PATH = "data/config.json"

# --- Pydantic-mallit vastauksille ---

class VideoResponse(BaseModel):
    id: int
    file_path: str
    status: str
    orig_size_bytes: int
    new_size_bytes: Optional[int]
    saved_bytes: int
    saved_percent: float
    notes: Optional[str]
    encode_bytes_written: Optional[int] = 0
    vmaf_bytes_written: Optional[int] = 0
    vmaf_storage_type: Optional[str] = None
    error_log: Optional[str] = None
    worker_node: Optional[str] = None

    class Config:
        from_attributes = True

class StatsResponse(BaseModel):
    total_videos: int
    status_counts: dict
    total_saved_gb: float
    total_encode_writes_gb: float
    total_vmaf_disk_writes_gb: float
    total_vmaf_ram_writes_gb: float

class EnqueueRequest(BaseModel):
    ids: Optional[List[int]] = None   # Tietyt ID:t
    all_pending: bool = False          # True = kaikki PENDING-tilaiset

class DequeueRequest(BaseModel):
    ids: Optional[List[int]] = None
    all_queued: bool = False

# --- Apufunktio taustaskannaukselle ---
def background_scan():
    config = load_config()
    db = next(get_db())
    try:
        scan_library(config, db)
    finally:
        db.close()

# --- Reitit ---

from core.progress import read_all_progress, clear_all_progress, update_job_progress, clear_job_progress

def _read_progress() -> dict:
    return read_all_progress()

def _compute_queue_eta(current_progress: dict) -> dict:
    """Arvioi jonon kokonaisvalmistumisajan historiallisen läpivientinopeuden (tavua/s) perusteella."""
    active = [JobStatus.QUEUED, JobStatus.PROCESSING]
    with SessionLocal() as db:
        remaining = db.query(func.coalesce(func.sum(VideoRecord.orig_size_bytes), 0)).filter(
            VideoRecord.status.in_(active)
        ).scalar() or 0
        count = db.query(VideoRecord).filter(VideoRecord.status.in_(active)).count()
        hist = db.query(
            func.coalesce(func.sum(VideoRecord.orig_size_bytes), 0),
            func.coalesce(func.sum(VideoRecord.processing_time_sec), 0)
        ).filter(
            VideoRecord.status.in_([JobStatus.COMPLETED, JobStatus.APPLIED]),
            VideoRecord.processing_time_sec > 0
        ).one()
        # Vähennetään käynnissä olevien töiden valmistuneet osuudet
        jobs = current_progress.get("jobs", {})
        if isinstance(jobs, dict) and jobs:
            for jid_str, jdata in jobs.items():
                pct = jdata.get("percent")
                if isinstance(pct, (int, float)):
                    try:
                        jid = int(jid_str)
                        rec = db.query(VideoRecord).filter(VideoRecord.id == jid).first()
                        if rec and rec.orig_size_bytes:
                            remaining -= int(rec.orig_size_bytes * pct / 100)
                    except (ValueError, TypeError):
                        pass
        elif isinstance(current_progress.get("percent"), (int, float)):
            cur = db.query(VideoRecord).filter(VideoRecord.status == JobStatus.PROCESSING).first()
            if cur and cur.orig_size_bytes:
                remaining -= int(cur.orig_size_bytes * current_progress["percent"] / 100)

    eta_sec = None
    if hist[1] and hist[1] > 0 and hist[0] > 0:
        throughput = hist[0] / hist[1]  # tavua/s
        eta_sec = int(max(0, remaining) / throughput)
    return {"count": count, "remaining_bytes": int(remaining), "eta_sec": eta_sec}

@router.get("/progress")
def get_progress():
    return _read_progress()

@router.get("/progress/stream")
async def progress_stream():
    """Server-Sent Events: lähettää progressin heti kun se muuttuu (+ jonon ETA)."""
    async def event_gen():
        last_payload = None
        last_queue_ts = 0.0
        queue_info: dict = {}
        idle_ticks = 0
        while True:
            prog = _read_progress()
            now = time.time()
            if now - last_queue_ts > 5:
                try:
                    queue_info = await asyncio.to_thread(_compute_queue_eta, prog)
                except Exception:
                    queue_info = {}
                last_queue_ts = now
            payload = json.dumps({**prog, "queue": queue_info}, sort_keys=True)
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 30:  # keepalive ~15 s välein
                    yield ": keepalive\n\n"
                    idle_ticks = 0
            await asyncio.sleep(0.5)
    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.post("/scan")
def trigger_scan(background_tasks: BackgroundTasks):
    """Käynnistää kirjaston skannauksen taustalla. Ei aloita enkoodausta."""
    background_tasks.add_task(background_scan)
    return {"message": "Skannaus käynnistetty taustalla."}

@router.get("/queue", response_model=List[VideoResponse])
def get_queue(status: Optional[str] = None, exclude: Optional[str] = None, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    """Palauttaa jonon tilanteen. Mahdollisuus rajata statuksella tai poissulkea statuksia."""
    query = db.query(VideoRecord)

    if status:
        if status.lower() == "active":
            query = query.filter(VideoRecord.status.notin_([JobStatus.APPLIED, JobStatus.SKIPPED]))
        else:
            query = query.filter(VideoRecord.status == status.lower())
    elif exclude:
        excluded_list = [e.strip().lower() for e in exclude.split(",") if e.strip()]
        if excluded_list:
            query = query.filter(VideoRecord.status.notin_(excluded_list))

    records = query.order_by(VideoRecord.created_at.desc()).offset(offset).limit(limit).all()
    return records

@router.get("/stats", response_model=StatsResponse)
def get_stats(db: Session = Depends(get_db)):
    """Palauttaa koontitilastot (säästetty tila, käsiteltyjen määrä, levykirjoitukset)."""

    total = db.query(VideoRecord).count()

    status_counts_db = db.query(VideoRecord.status, func.count(VideoRecord.id)).group_by(VideoRecord.status).all()
    status_counts = {status.value: count for status, count in status_counts_db}

    # Varmistetaan että kaikki statukset näkyvät vastauksessa
    for s in JobStatus:
        if s.value not in status_counts:
            status_counts[s.value] = 0

    completed_videos = db.query(VideoRecord).filter(VideoRecord.status.in_([JobStatus.COMPLETED, JobStatus.APPLIED])).all()
    saved_bytes_total = sum(v.saved_bytes for v in completed_videos)
    saved_gb = saved_bytes_total / (1024**3)

    io_stats = get_io_stats()
    db_enc_bytes = sum(getattr(v, "encode_bytes_written", None) or v.new_size_bytes or 0 for v in completed_videos)
    db_vmaf_disk_bytes = sum(getattr(v, "vmaf_bytes_written", 0) or 0 for v in completed_videos if getattr(v, "vmaf_storage_type", "ram") == "disk")
    db_vmaf_ram_bytes = sum(getattr(v, "vmaf_bytes_written", 0) or 0 for v in completed_videos if getattr(v, "vmaf_storage_type", "ram") != "disk" and (getattr(v, "vmaf_bytes_written", 0) or 0) > 0)

    enc_bytes = max(io_stats.get("encode_disk_bytes", 0), db_enc_bytes)
    vmaf_disk_bytes = max(io_stats.get("vmaf_disk_bytes", 0), db_vmaf_disk_bytes)
    vmaf_ram_bytes = max(io_stats.get("vmaf_ram_bytes", 0), db_vmaf_ram_bytes)

    return {
        "total_videos": total,
        "status_counts": status_counts,
        "total_saved_gb": round(saved_gb, 2),
        "total_encode_writes_gb": round(enc_bytes / (1024**3), 2),
        "total_vmaf_disk_writes_gb": round(vmaf_disk_bytes / (1024**3), 2),
        "total_vmaf_ram_writes_gb": round(vmaf_ram_bytes / (1024**3), 2)
    }

@router.post("/stats/reset-writes")
def reset_writes(db: Session = Depends(get_db)):
    """Nollaa levylle/RAM:iin kirjoitetun datan tilastot."""
    reset_io_stats()
    
    # Tyhjennetään myös tietokannasta vanhojen videoiden luvut yhdellä SQL-kyselyllä
    db.query(VideoRecord).update(
        {VideoRecord.encode_bytes_written: 0, VideoRecord.vmaf_bytes_written: 0},
        synchronize_session=False
    )
    db.commit()
    
    return {"status": "ok", "message": "Kirjoitetun datan tilastot nollattu"}


@router.get("/system")
def get_system():
    """Reaaliaikainen CPU/RAM/levy/GPU-käyttö."""
    paths = []
    try:
        cfg = load_config()
        for f in cfg.files.folders:
            paths.append(f.input_dir)
            paths.append(f.output_dir)
        paths.append(cfg.files.temp_dir)
    except Exception:
        pass
    return get_system_stats(paths or ["/"])

# --- Enkoodauksen stop/pause/resume ohjaus ---

_CONTROL_FILE = Path(os.environ.get("OPTIVMAF_CONTROL_FILE", "data/control.json"))
_claim_lock = threading.Lock()

def _write_control(state: str):
    try:
        Path(_CONTROL_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(_CONTROL_FILE, "w") as f:
            json.dump({"state": state, "ts": time.time()}, f)
    except:
        pass

@router.post("/control/stop")
def control_stop():
    """Pysäyttää nykyisen enkoodauksen kokonaan (lopettaa ffmpeg-prosessin)."""
    _write_control("stop")
    return {"message": "Stop-signaali lähetetty workerille."}

@router.post("/control/pause")
def control_pause():
    """Pysäyttää enkoodauksen tilapäisesti — voidaan jatkaa resume:lla."""
    _write_control("pause")
    return {"message": "Pause-signaali lähetetty workerille."}

@router.post("/control/resume")
def control_resume():
    """Jatkaa pysäytettyä enkoodausta."""
    _write_control("run")
    return {"message": "Resume-signaali lähetetty workerille."}

@router.get("/control/state")
def control_state():
    """Palauttaa nykyisen kontrollitilan."""
    if os.path.exists(_CONTROL_FILE):
        try:
            with open(_CONTROL_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"state": "run"}

# --- Enkoodauksen ohjaus ---

@router.post("/queue/enqueue")
def enqueue_jobs(req: EnqueueRequest, db: Session = Depends(get_db)):
    """Siirtää valitut tai kaikki PENDING-tilaiset videot QUEUED-tilaan (enkoodausjono)."""
    # Sallitaan myös epäonnistuneiden (FAILED) ja ohitettujen (SKIPPED) lisääminen suoraan jonoon
    query = db.query(VideoRecord).filter(VideoRecord.status.in_([JobStatus.PENDING, JobStatus.FAILED, JobStatus.SKIPPED]))

    if req.all_pending:
        records = db.query(VideoRecord).filter(VideoRecord.status == JobStatus.PENDING).all()
    elif req.ids:
        records = query.filter(VideoRecord.id.in_(req.ids)).all()
    else:
        raise HTTPException(status_code=400, detail="Anna joko 'ids' tai 'all_pending: true'")

    count = 0
    for r in records:
        r.status = JobStatus.QUEUED
        count += 1

    db.commit()
    return {"message": f"{count} videota siirretty enkoodausjonoon."}

@router.post("/queue/dequeue")
def dequeue_jobs(req: DequeueRequest, db: Session = Depends(get_db)):
    """Peruuttaa enkoodauksen — siirtää QUEUED → PENDING."""
    query = db.query(VideoRecord).filter(VideoRecord.status == JobStatus.QUEUED)

    if req.all_queued:
        records = query.all()
    elif req.ids:
        records = query.filter(VideoRecord.id.in_(req.ids)).all()
    else:
        raise HTTPException(status_code=400, detail="Anna joko 'ids' tai 'all_queued: true'")

    count = 0
    for r in records:
        r.status = JobStatus.PENDING
        count += 1

    db.commit()
    return {"message": f"{count} videota poistettu enkoodausjonosta."}

# --- Config-hallinta ---

@router.get("/config")
def get_config():
    """Palauttaa nykyisen config.json-sisällön täydennettynä oletusarvoilla."""
    try:
        config_obj = load_config()
        return json.loads(config_obj.model_dump_json())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="config.json ei löydy")

@router.put("/config")
def save_config(data: dict):
    """Tallentaa uuden config-sisällön config.json-tiedostoon."""
    # Validoidaan rakenne lataamalla pydantic-mallilla ennen tallennusta
    try:
        from core.config import AppConfig
        AppConfig(**data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Asetusvirhe: {e}")

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return {"message": "Asetukset tallennettu."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tallennus epäonnistui: {e}")

# --- Yksittäisten töiden hallinta ---

def _encoded_output_path(job: VideoRecord, config) -> Path:
    """Palauttaa COMPLETED-työn valmiin enkoodatun tiedoston polun output_dir:ssä."""
    ext = ".mkv"
    if job.encoder_used and job.encoder_used.startswith("remux_"):
        ext = "." + job.encoder_used.split("_")[1]
    
    folder = next((f for f in config.files.folders if f.id == job.folder_id), None)
    if not folder:
        raise HTTPException(status_code=500, detail=f"Kansioasetusta (id={job.folder_id}) ei löytynyt.")
        
    rel_path = job.file_path.split(":", 1)[1] if ":" in job.file_path else job.file_path
    return folder.output_dir / Path(rel_path).with_suffix(ext)

def _prune_empty_dirs(current_dir: Path, root_dir: Path):
    """Puhdistaa tyhjät kansiot ylöspäin root_dir:iin asti."""
    try:
        root_dir = root_dir.resolve()
        current_dir = current_dir.resolve()
        while current_dir != root_dir and root_dir in current_dir.parents:
            if not any(current_dir.iterdir()):
                current_dir.rmdir()
                current_dir = current_dir.parent
            else:
                break
    except Exception:
        pass

def _delete_encoded_file(job: VideoRecord, config) -> bool:
    """Poistaa valmiin enkoodatun tiedoston levyltä. Vain COMPLETED-tilaisille
    (APPLIED-tilassa tiedosto on jo siirretty alkuperäisen paikalle eikä sitä poisteta)."""
    if job.status != JobStatus.COMPLETED:
        return False
    out = _encoded_output_path(job, config)
    try:
        if out.is_file():
            out.unlink()
            
            # Puhdistetaan tyhjät hakemistot poiston jälkeen
            folder = next((f for f in config.files.folders if f.id == job.folder_id), None)
            if folder:
                _prune_empty_dirs(out.parent, folder.output_dir)
                
            return True
    except Exception:
        pass
    return False

@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, delete_file: bool = False, db: Session = Depends(get_db)):
    """Poistaa yksittäisen työn tietokannasta. delete_file=true poistaa myös valmiin
    enkoodatun tiedoston levyltä (alkuperäistä ei koskaan poisteta)."""
    job = db.query(VideoRecord).filter(VideoRecord.id == job_id).first()
    if not job:
        return {"error": "Työtä ei löytynyt"}

    file_deleted = False
    if delete_file:
        try:
            file_deleted = _delete_encoded_file(job, load_config())
        except Exception:
            pass

    db.delete(job)
    db.commit()
    msg = f"Työ {job_id} poistettu."
    if delete_file:
        msg += " Enkoodattu tiedosto poistettu levyltä." if file_deleted else " Enkoodattua tiedostoa ei poistettu (ei löytynyt)."
    return {"message": msg, "file_deleted": file_deleted}

@router.delete("/reset")
def reset_database():
    session = SessionLocal()
    try:
        session.query(VideoRecord).delete()
        session.commit()
        return {"status": "ok", "message": "Tietokanta tyhjennetty"}
    finally:
        session.close()

# --- Uudelleenkoodaus ja korvaus ---

@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, db: Session = Depends(get_db)):
    """Nollaa työn takaisin PENDING-tilaan uudelleenkoodausta varten.
    Tyhjentää tuloskentät (koko, VMAF, aika) niin kuin työtä ei olisi koskaan käsitelty."""
    job = db.query(VideoRecord).filter(VideoRecord.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Työtä ei löytynyt")
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(status_code=409, detail="Työ on parhaillaan käynnissä — pysäytä enkoodaus ensin")

    job.status = JobStatus.PENDING
    job.new_size_bytes = None
    job.processing_time_sec = None
    job.encoder_used = None
    job.vmaf_score = None
    job.notes = None
    db.commit()
    return {"message": f"Työ {job_id} nollattu uudelleenkoodausta varten."}

@router.post("/jobs/{job_id}/apply")
def apply_job(job_id: int, db: Session = Depends(get_db)):
    """Korvaa alkuperäisen tiedoston enkoodatulla versiolla (kun in_place=False).
    Siirtää output_dir:ssä olevan .mkv-tiedoston alkuperäisen paikalle ja poistaa alkuperäisen."""
    job = db.query(VideoRecord).filter(VideoRecord.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Työtä ei löytynyt")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Vain COMPLETED-tila voidaan soveltaa")

    try:
        config = load_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Config-lataus epäonnistui: {e}")

    ext = ".mkv"
    if job.encoder_used and job.encoder_used.startswith("remux_"):
        ext = "." + job.encoder_used.split("_")[1]

    folder = next((f for f in config.files.folders if f.id == job.folder_id), None)
    if not folder:
        raise HTTPException(status_code=500, detail=f"Kansioasetusta (id={job.folder_id}) ei löytynyt.")
        
    rel_path = job.file_path.split(":", 1)[1] if ":" in job.file_path else job.file_path

    input_path  = folder.input_dir  / rel_path
    output_path = folder.output_dir / Path(rel_path).with_suffix(ext)

    if not output_path.exists():
        raise HTTPException(status_code=404,
            detail=f"Enkoodattua tiedostoa ei löydy: {output_path}")

    # Poistetaan alkuperäinen ja siirretään enkoodattu tilalle
    try:
        input_path.unlink(missing_ok=True)
        shutil.move(str(output_path), str(input_path.with_suffix(ext)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tiedoston siirto epäonnistui: {e}")

    job.notes = (job.notes or "") + " | Korvattu alkuperäinen"
    job.status = JobStatus.APPLIED
    db.commit()
    return {"message": f"Enkoodattu tiedosto siirretty korvaamaan alkuperäinen: {input_path.with_suffix(ext)}"}

# --- BULK TOIMINNOT ---

class JobIdsRequest(BaseModel):
    ids: list[int]

@router.post("/jobs/bulk-retry")
def bulk_retry_jobs(req: JobIdsRequest, db: Session = Depends(get_db)):
    count = 0
    for job_id in req.ids:
        job = db.query(VideoRecord).filter(VideoRecord.id == job_id).first()
        if job and job.status != JobStatus.PROCESSING:
            job.status = JobStatus.PENDING
            job.new_size_bytes = None
            job.processing_time_sec = None
            job.encoder_used = None
            job.vmaf_score = None
            job.notes = None
            count += 1
    db.commit()
    return {"message": f"{count} työtä nollattu uudelleenkoodausta varten."}

@router.post("/jobs/bulk-apply")
def bulk_apply_jobs(req: JobIdsRequest, db: Session = Depends(get_db)):
    config = load_config()
    count = 0
    errors = 0
    total = len(req.ids)
    
    print(f"[Bulk Apply] Aloitetaan {total} videon tallennus...")
    
    for i, job_id in enumerate(req.ids):
        job = db.query(VideoRecord).filter(VideoRecord.id == job_id).first()
        if job and job.status == JobStatus.COMPLETED:
            # Päivitetään UI progress
            update_job_progress("bulk_apply", {"file": f"Tallennetaan {i+1}/{total}...", "fps": "Siirto", "time": f"{i+1}/{total}", "speed": "levylle"})
                
            ext = ".mkv"
            if job.encoder_used and job.encoder_used.startswith("remux_"):
                ext = "." + job.encoder_used.split("_")[1]
                
            folder = next((f for f in config.files.folders if f.id == job.folder_id), None)
            if not folder:
                errors += 1
                continue
                
            rel_path = job.file_path.split(":", 1)[1] if ":" in job.file_path else job.file_path
            print(f"[Bulk Apply] ({i+1}/{total}) Siirretään: {rel_path}")
            
            input_path  = folder.input_dir  / rel_path
            output_path = folder.output_dir / Path(rel_path).with_suffix(ext)
            
            if output_path.exists():
                try:
                    input_path.unlink(missing_ok=True)
                    shutil.move(str(output_path), str(input_path.with_suffix(ext)))
                    job.status = JobStatus.APPLIED
                    job.notes = (job.notes or "") + " | Korvattu alkuperäinen"
                    count += 1
                    
                    # Puhdistetaan tyhjät hakemistot output_dir:stä
                    _prune_empty_dirs(output_path.parent, folder.output_dir)
                    
                except Exception as e:
                    print(f"[Bulk Apply] VIRHE tiedostossa {rel_path}: {e}")
                    errors += 1
            elif input_path.with_suffix(ext).exists():
                print(f"[Bulk Apply] Tiedosto {rel_path} oli jo paikallaan.")
                job.status = JobStatus.APPLIED
                if "Korvattu alkuperäinen" not in (job.notes or ""):
                    job.notes = (job.notes or "") + " | Korvattu alkuperäinen (Paikattu)"
                count += 1
            else:
                print(f"[Bulk Apply] VIRHE: Lähdetiedostoa ei löytynyt {output_path}")
                errors += 1
                
    db.commit()
    print(f"[Bulk Apply] Valmis. {count} onnistui, {errors} virhettä.")
    
    # Tyhjennetään progress
    clear_all_progress()
        
    return {"message": f"{count} tiedostoa korvattu." + (f" ({errors} virhettä)" if errors > 0 else "")}

class BulkDeleteRequest(BaseModel):
    ids: list[int]
    delete_files: bool = False  # Poista myös valmiit enkoodatut tiedostot levyltä

@router.post("/jobs/bulk-delete")
def bulk_delete_jobs(req: BulkDeleteRequest, db: Session = Depends(get_db)):
    files_deleted = 0
    if req.delete_files:
        try:
            config = load_config()
            jobs = db.query(VideoRecord).filter(VideoRecord.id.in_(req.ids)).all()
            for job in jobs:
                if _delete_encoded_file(job, config):
                    files_deleted += 1
        except Exception:
            pass
    count = db.query(VideoRecord).filter(VideoRecord.id.in_(req.ids)).delete(synchronize_session=False)
    db.commit()
    msg = f"{count} työtä poistettu."
    if req.delete_files:
        msg += f" {files_deleted} enkoodattua tiedostoa poistettu levyltä."
    return {"message": msg}

@router.post("/jobs/reset-stuck")
def reset_stuck_jobs(db: Session = Depends(get_db)):
    """Nollaa kaikki PROCESSING-tilassa olevat työt takaisin QUEUED:ksi.
    Käytetään kun API/kontti on käynnistetty uudelleen kesken enkoodauksen,
    jolloin PROCESSING-tila jää jumiin (enkoodausprosessi on jo kuollut)."""
    stuck = db.query(VideoRecord).filter(VideoRecord.status == JobStatus.PROCESSING).all()
    count = len(stuck)
    for job in stuck:
        job.status = JobStatus.QUEUED
        job.notes = (job.notes or "") + " | Nollattu (kontin restart)"
    db.commit()
    return {"message": f"{count} jumissa olevaa työtä nollattu takaisin jonoon."}

# --- HAJAUTETUT TYÖASEMAT (NODES) ---

class NodeHeartbeatRequest(BaseModel):
    node_id: str
    node_name: Optional[str] = None
    encoder: Optional[str] = None
    speedpreset: Optional[Union[str, int]] = None
    status: Optional[str] = "idle"
    current_job_id: Optional[int] = None
    hostname: Optional[str] = None
    system_info: Optional[dict] = None

class NodeProgressRequest(BaseModel):
    job_id: int
    file: Optional[str] = None
    percent: Optional[float] = None
    fps: Optional[str] = None
    speed: Optional[str] = None
    time: Optional[str] = None
    eta_sec: Optional[int] = None
    node: Optional[str] = None
    q_label: Optional[str] = None
    q_value: Optional[int] = None
    vmaf_testing: Optional[bool] = False
    vmaf_current_icq: Optional[int] = None
    vmaf_candidates: Optional[list] = None
    vmaf_results: Optional[list] = None

class NodeCompleteRequest(BaseModel):
    job_id: int
    new_size_bytes: int
    processing_time_sec: float
    encoder_used: str
    vmaf_score: Optional[float] = None
    notes: Optional[str] = None
    encode_bytes_written: Optional[int] = 0
    vmaf_bytes_written: Optional[int] = 0
    vmaf_storage_type: Optional[str] = "disk"
    status: Optional[str] = "completed"

class NodeFailRequest(BaseModel):
    job_id: int
    error: str
    error_log: Optional[str] = None

class NodeReleaseRequest(BaseModel):
    job_id: int
    reason: Optional[str] = None

@router.get("/nodes")
def list_nodes():
    """Listaa kaikki rekisteröidyt työasemat ja niiden tilat."""
    nodes = get_all_nodes()
    # Jos palvelinta itseään ei ole vielä rekisteröity, lisätään se näkyviin oletuksena
    has_server = any(n.get("node_id") in ("server", "local") for n in nodes)
    if not has_server:
        try:
            cfg = load_config()
            enc = cfg.video.encoder
        except Exception:
            enc = "av1_vaapi"
        nodes.insert(0, {
            "node_id": "server",
            "node_name": "Palvelin (Master)",
            "encoder": enc,
            "speedpreset": getattr(cfg.video, "encoder_speedpreset", None) if "cfg" in locals() else None,
            "status": "active",
            "is_online": True,
            "seconds_since_heartbeat": 0,
            "total_jobs_completed": 0
        })
    return {"nodes": nodes}

@router.post("/nodes/heartbeat")
def node_heartbeat(req: NodeHeartbeatRequest, raw_req: Request):
    """Vastaanottaa työaseman heartbeat-signaalin ja palauttaa palvelimen kontrollitilan."""
    client_ip = raw_req.client.host if raw_req.client else ""
    data = req.dict() if hasattr(req, "dict") else req.model_dump()
    register_or_update_node(req.node_id, data, client_ip=client_ip)

    ctrl = "run"
    if os.path.exists(_CONTROL_FILE):
        try:
            with open(_CONTROL_FILE, "r") as f:
                ctrl = json.load(f).get("state", "run")
        except Exception:
            pass
    return {"status": "ok", "control": ctrl}

@router.post("/nodes/{node_id}/claim-job")
def claim_job(node_id: str, db: Session = Depends(get_db)):
    """Ulkoinen worker (esim. peli-PC) varaa seuraavan enkoodaustyön jonosta."""
    with _claim_lock:
        if os.path.exists(_CONTROL_FILE):
            try:
                with open(_CONTROL_FILE, "r") as f:
                    if json.load(f).get("state") in ("stop", "pause"):
                        return {"job": None, "message": "Jono on tauolla tai pysäytetty."}
            except Exception:
                pass

        job = (
            db.query(VideoRecord)
            .filter(VideoRecord.status == JobStatus.QUEUED)
            .order_by(VideoRecord.created_at.asc())
            .first()
        )
        if not job:
            return {"job": None}

        config = load_config()
        folder = next((f for f in config.files.folders if f.id == job.folder_id), None)
        if not folder:
            job.status = JobStatus.FAILED
            job.notes = f"Kansio {job.folder_id} puuttuu asetuksista."
            db.commit()
            return {"job": None, "error": f"Kansiota {job.folder_id} ei löytynyt."}

        rel_path = job.file_path.split(":", 1)[1] if ":" in job.file_path else job.file_path

        job.status = JobStatus.PROCESSING
        job.worker_node = node_id
        db.commit()

        update_node_job(node_id, job.id, "encoding")

        return {
            "job": {
                "id": job.id,
                "folder_id": job.folder_id,
                "file_path": job.file_path,
                "rel_path": rel_path,
                "input_dir": str(folder.input_dir),
                "output_dir": str(folder.output_dir),
                "in_place": config.files.in_place,
                "orig_codec": job.orig_codec,
                "orig_size_bytes": job.orig_size_bytes,
                "orig_height": job.orig_height,
                "notes": job.notes,
                "vmaf_score": job.vmaf_score,
                "config": {
                    **json.loads(config.model_dump_json()).get("video", {}),
                    **json.loads(config.model_dump_json()).get("audio_subs", {}),
                    **json.loads(config.model_dump_json()).get("container", {}),
                    **json.loads(config.model_dump_json()).get("safety", {}),
                    "video": json.loads(config.model_dump_json()).get("video", {}),
                    "audio_subs": json.loads(config.model_dump_json()).get("audio_subs", {}),
                    "container": json.loads(config.model_dump_json()).get("container", {}),
                    "safety": json.loads(config.model_dump_json()).get("safety", {})
                }
            }
        }

@router.post("/nodes/{node_id}/progress")
def report_progress(node_id: str, req: NodeProgressRequest):
    """Päivittää ulkoisen noden työn edistymisen progress.json-tilakenttään."""
    data = req.dict() if hasattr(req, "dict") else req.model_dump()
    data["node"] = req.node or node_id
    update_job_progress(req.job_id, data)
    register_or_update_node(node_id, {"status": "encoding", "current_job_id": req.job_id})

    ctrl = "run"
    if os.path.exists(_CONTROL_FILE):
        try:
            with open(_CONTROL_FILE, "r") as f:
                ctrl = json.load(f).get("state", "run")
        except Exception:
            pass
    return {"status": "ok", "control": ctrl}

@router.post("/nodes/{node_id}/complete")
def report_complete(node_id: str, req: NodeCompleteRequest, db: Session = Depends(get_db)):
    """Kuittaa ulkoisen noden enkoodaaman työn valmiiksi tietokantaan."""
    job = db.query(VideoRecord).filter(VideoRecord.id == req.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Työtä ei löytynyt")

    job.status = JobStatus.COMPLETED if req.status == "completed" else JobStatus.SKIPPED
    job.new_size_bytes = req.new_size_bytes
    job.processing_time_sec = req.processing_time_sec
    job.encoder_used = req.encoder_used
    job.vmaf_score = req.vmaf_score
    job.notes = req.notes or f"Valmis (Node: {node_id})"
    job.worker_node = node_id
    job.encode_bytes_written = req.encode_bytes_written or 0
    job.vmaf_bytes_written = req.vmaf_bytes_written or 0
    job.vmaf_storage_type = req.vmaf_storage_type or "disk"

    if req.encode_bytes_written:
        add_io_stats(encode_disk_bytes=req.encode_bytes_written)
    if req.vmaf_bytes_written:
        if req.vmaf_storage_type == "ram":
            add_io_stats(vmaf_ram_bytes=req.vmaf_bytes_written)
        else:
            add_io_stats(vmaf_disk_bytes=req.vmaf_bytes_written)

    db.commit()
    record_node_completed(node_id)
    clear_job_progress(req.job_id)
    return {"status": "ok", "message": f"Työ {req.job_id} kuitattu valmiiksi."}

@router.post("/nodes/{node_id}/fail")
def report_fail(node_id: str, req: NodeFailRequest, db: Session = Depends(get_db)):
    """Kirjaa ulkoisen noden raportoiman virheen tietokantaan."""
    job = db.query(VideoRecord).filter(VideoRecord.id == req.job_id).first()
    if job:
        job.status = JobStatus.FAILED
        job.notes = f"Virhe ({node_id}): {req.error}"
        job.error_log = req.error_log
        job.worker_node = node_id
        db.commit()
    record_node_completed(node_id)
    clear_job_progress(req.job_id)
    return {"status": "ok", "message": f"Työ {req.job_id} merkitty epäonnistuneeksi."}

@router.post("/nodes/{node_id}/release")
def release_job(node_id: str, req: NodeReleaseRequest, db: Session = Depends(get_db)):
    """Vapauttaa varatun työn takaisin jonoon (esim. peli-PC suljetaan tai aloitetaan pelaaminen)."""
    job = db.query(VideoRecord).filter(VideoRecord.id == req.job_id).first()
    if job and job.status == JobStatus.PROCESSING:
        job.status = JobStatus.QUEUED
        job.worker_node = None
        reason_str = f" | Vapautettu jonoon: {req.reason}" if req.reason else " | Vapautettu jonoon"
        job.notes = (job.notes or "") + reason_str
        db.commit()
    update_node_job(node_id, None, "idle")
    clear_job_progress(req.job_id)
    return {"status": "ok", "message": f"Työ {req.job_id} palautettu jonoon."}

