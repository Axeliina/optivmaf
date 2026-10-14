import os
import shutil
import time
import subprocess
import re
import signal
import json
from pathlib import Path
from sqlalchemy.orm import Session

from core.config import AppConfig, load_config
from core.database import VideoRecord, JobStatus, SessionLocal
from core.progress import update_job_progress, clear_job_progress
from workers.probe import probe_video, get_duration, filter_streams 
from workers.encoder import build_ffmpeg_cmd, find_best_icq, EncoderError
from core.stats import add_io_stats

_CONTROL_FILE = Path(os.environ.get("OPTIVMAF_CONTROL_FILE", "data/control.json"))

def _read_control_state() -> str:
    """Lukee kontrollitilan control.json-tiedostosta. Palauttaa 'run', 'pause' tai 'stop'."""
    try:
        if Path(_CONTROL_FILE).exists():
            with open(_CONTROL_FILE, "r") as f:
                data = json.load(f)
                return data.get("state", "run")
    except:
        pass
    return "run"

def _reset_control():
    """Nollaa kontrollitilan 'run'-tilaan enkoodauksen päätyttyä."""
    try:
        Path(_CONTROL_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(_CONTROL_FILE, "w") as f:
            json.dump({"state": "run"}, f)
    except:
        pass

class JobManagerError(Exception):
    pass

def check_disk_space(output_dir: Path, min_free_gb: float) -> bool:
    total, used, free = shutil.disk_usage(output_dir)
    return (free / (1024**3)) >= min_free_gb

def cleanup_orphaned_temp(config: AppConfig):
    """Siivoaa aiemmin katkenneilta ajoilta jääneet orvot väliaikaistiedostot."""
    temp_dir = config.files.temp_dir
    if temp_dir.exists():
        for p in temp_dir.glob("encode_*.tmp.mkv"):
            try:
                print(f"[STARTUP] Poistetaan orpo väliaikaistiedosto levyltä: {p}")
                p.unlink(missing_ok=True)
            except Exception as e:
                print(f"[STARTUP] Ei voitu poistaa {p}: {e}")

    shm_dir = Path("/dev/shm")
    if shm_dir.exists():
        for p in shm_dir.glob("optivmaf_vmaf_*"):
            try:
                print(f"[STARTUP] Poistetaan orpo VMAF-hakemisto RAM-muistista: {p}")
                shutil.rmtree(p, ignore_errors=True)
            except Exception as e:
                print(f"[STARTUP] Ei voitu poistaa {p}: {e}")

def atomic_move(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def _remux_stream_copy(input_path: Path, output_path: Path) -> bool:
    """Remuxaa tiedosto uuteen konttiin stream-copy:lla (ei laadun menetystä).
    Palauttaa True jos onnistuu, False jos epäonnistuu."""
    import subprocess
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", str(input_path),
        "-c", "copy",
        "-map", "0",
        "-dn",
        "-avoid_negative_ts", "make_zero",
        "-map_chapters", "0",
        str(output_path)
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"  [REMUX VIRHE] {result.stderr[-300:]}")
        return False
    return True

def process_single_job(record: VideoRecord, session: Session, config: AppConfig):
    start_time = time.time()
    
    folder = next((f for f in config.files.folders if f.id == record.folder_id), None)
    if not folder:
        print(f"[VIRHE] Kansion asetuksia (id='{record.folder_id}') ei löytynyt asetuksista.")
        record.status = JobStatus.FAILED
        record.notes = f"Kansio {record.folder_id} puuttuu asetuksista."
        session.commit()
        return

    rel_path_str = record.file_path.split(":", 1)[1] if ":" in record.file_path else record.file_path
    
    input_path = folder.input_dir / rel_path_str
    
    if config.files.in_place:
        final_output_path = input_path.with_suffix(".mkv")
    else:
        final_output_path = folder.output_dir / Path(rel_path_str).with_suffix(".mkv")
        
    temp_path = config.files.temp_dir / f"encode_{record.id}.tmp.mkv"
    
    try:
        if not input_path.exists():
            raise JobManagerError(f"Tiedostoa ei löydy: {input_path}")
            
        record.status = JobStatus.PROCESSING
        session.commit()
        
        res = probe_video(input_path)
        if len(res) >= 8:
            dur, vid_idx, vid_codec, color_space, height, width, audios, subs = res[:8]
        else:
            dur, vid_idx, vid_codec, color_space, height, audios, subs = res
            width = 0

        streams = {
            "vid_idx": vid_idx,
            "codec_name": vid_codec,
            "color_space": color_space,
            "height": height,
            "width": width,
            "video": [{"height": height, "width": width}],
            "audios": audios,
            "subs": subs
        }
        
        record.orig_size_bytes = input_path.stat().st_size
        
        if vid_codec in config.video.skip_codecs:
            record.status = JobStatus.SKIPPED
            record.notes = f"Koodekki ohitettu ({vid_codec})"
            session.commit()
            return
            
        streams['audios'], streams['subs'] = filter_streams(
            audios, subs, config.audio_subs.allowed_languages
        )

        reused_icq = None
        reused_vmaf = record.vmaf_score or 0.0
        if record.notes and ("ICQ:" in record.notes or "CRF:" in record.notes or "CQ:" in record.notes):
            match = re.search(r"(ICQ|CRF|CQ):\s*(\d+)", record.notes)
            if match:
                reused_icq = int(match.group(2))
                print(f"-> Aiempi VMAF-testin tulos löytyi ({match.group(1)} {reused_icq}), ohitetaan VMAF-laskenta.")

        if reused_icq is not None:
            best_icq = reused_icq
            best_vmaf = reused_vmaf
            color_filter = None
            vmaf_info = {"bytes_written": 0, "storage": "none"}
        else:
            best_icq, best_vmaf, color_filter, vmaf_info = find_best_icq(
                input_path, streams, dur, config,
                progress_base={"file": record.file_path, "job_id": record.id, "node": "Palvelin"}
            )
        record.vmaf_bytes_written = vmaf_info.get("bytes_written", 0)
        record.vmaf_storage_type = vmaf_info.get("storage", "ram")
        session.commit()
        if record.vmaf_storage_type == "disk":
            add_io_stats(vmaf_disk_bytes=record.vmaf_bytes_written)
        elif record.vmaf_storage_type == "ram":
            add_io_stats(vmaf_ram_bytes=record.vmaf_bytes_written)

        enc_name = config.video.encoder.lower()
        if "nvenc" in enc_name:
            q_label = "CQ"
        elif "vaapi" in enc_name:
            q_label = "ICQ"
        else:
            q_label = "CRF"
        # Jos mikään arvo ei saavuttanut min-VMAF — ohitetaan video
        if best_icq is None:
            record.status = JobStatus.SKIPPED
            record.notes = f"VMAF liian alhainen kaikilla {q_label}-arvoilla (min {config.video.min_vmaf})"
            session.commit()
            print(f"[OHITETTU] {record.file_path}: laatu/koko-suhde ei riittävä AV1-enkoodaukseen.")
            if config.video.remux_on_skip:
                ext = config.video.remux_container.lstrip(".")
                remux_out = folder.output_dir / Path(rel_path_str).with_suffix(f".{ext}")
                print(f"  -> remux_on_skip=True: remuxataan {input_path.suffix} → .{ext} (stream-copy)...")
                if _remux_stream_copy(input_path, remux_out):
                    print(f"  -> Remux valmis: {remux_out}")
                    remux_size = remux_out.stat().st_size if remux_out.exists() else 0
                    record.status = JobStatus.COMPLETED
                    record.encoder_used = f"remux_{ext}"
                    record.new_size_bytes = remux_size
                    record.encode_bytes_written = remux_size
                    record.notes = f"VMAF liian alhainen | Remuxattu .{ext}"
                    session.commit()
                    add_io_stats(encode_disk_bytes=remux_size)
                else:
                    print(f"  -> Remux epäonnistui.")
            return

        record.vmaf_score = best_vmaf
        record.notes = f"{q_label}: {best_icq}"

        cmd = build_ffmpeg_cmd(
            input_path=input_path, output_path=temp_path,
            streams=streams, config=config,
            icq_crf=best_icq, is_vmaf_test=False,
            color_filter=color_filter
        )

        print(f"\n{'='*65}")
        print(f"Käsitellään: {record.file_path}")
        print(f"{'-'*65}")
        print(f"-> Koodekki: {vid_codec} | Resoluutio: {height}p")
        print(f"-> Ääniraidat: {len(streams['audios'])} | Tekstit: {len(streams['subs'])}")
        print(f"*** Valittu {q_label} {best_icq} (VMAF {best_vmaf:.2f}) ***")
        print(f"-> FFmpeg: {' '.join(cmd)}")
        
        proc = subprocess.Popen(cmd, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, text=True, universal_newlines=True)
        
        last_update = 0
        _v0 = (streams.get("video") or [{}])[0]
        job_info = {
            "src_codec": vid_codec,
            "width": width or _v0.get("width") or None,
            "height": height or None,
            "q_label": q_label,
            "q_value": best_icq,
            "vmaf": round(best_vmaf, 2) if best_vmaf else None,
            "encoder": config.video.encoder,
            "orig_size_bytes": record.orig_size_bytes,
            "node": "Palvelin",
        }
        error_log = [] # Tähän kerätään FFmpegin tulostetta virhetilanteen varalta
        
        for line in proc.stdout:
            error_log.append(line.strip())
            if len(error_log) > 30:
                error_log.pop(0) # Pidetään muistissa vain viimeiset 30 riviä

            # --- Kontrollisignaalien tarkistus ---
            ctrl = _read_control_state()
            if ctrl == "stop":
                print("\n[CTRL] Stop-signaali vastaanotettu — lopetetaan ffmpeg.")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # Säilytetään stop-tila, jotta muutkin rinnakkaissäikeet pysähtyvät ja jono odottaa resume-signaalia
                # Siirretään työ takaisin jonoon uudelleenyritystä varten
                record.status = JobStatus.QUEUED
                session.commit()
                if temp_path.exists():
                    p_bytes = temp_path.stat().st_size
                    if p_bytes > 0:
                        add_io_stats(encode_disk_bytes=p_bytes)
                    temp_path.unlink()
                raise EncoderError("Käyttäjä pysäytti enkoodauksen (Stop).")
            elif ctrl == "pause":
                if hasattr(proc, 'pid'):
                    try:
                        os.kill(proc.pid, signal.SIGSTOP)
                        print("\n[CTRL] Pause — ffmpeg pysäytetty (SIGSTOP). Odotetaan resume...")
                    except Exception:
                        pass
                # Odotetaan kunnes tila muuttuu
                while True:
                    time.sleep(0.5)
                    s = _read_control_state()
                    if s == "run":
                        try:
                            os.kill(proc.pid, signal.SIGCONT)
                            print("[CTRL] Resume — ffmpeg jatkuu (SIGCONT).")
                        except Exception:
                            pass
                        break
                    elif s == "stop":
                        try:
                            os.kill(proc.pid, signal.SIGCONT)  # Jatketaan ennen terminointia
                            time.sleep(0.1)
                        except Exception:
                            pass
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        record.status = JobStatus.QUEUED
                        session.commit()
                        if temp_path.exists():
                            p_bytes = temp_path.stat().st_size
                            if p_bytes > 0:
                                add_io_stats(encode_disk_bytes=p_bytes)
                            temp_path.unlink()
                        raise EncoderError("Käyttäjä pysäytti enkoodauksen (Stop pausesta).")

            if "time=" in line and "fps=" in line:
                time_match = re.search(r"time=(\d{2}:\d{2}:\d{2})", line)
                fps_match = re.search(r"fps=\s*([\d\.]+)", line)
                speed_match = re.search(r"speed=\s*([\d\.x]+)", line)
                
                if time_match and fps_match and speed_match:
                    t = time_match.group(1)
                    fps = fps_match.group(1)
                    speed = speed_match.group(1)
                    
                    print(f"\r-> [Enkoodaus käynnissä] Aika: {t} | FPS: {fps} | Nopeus: {speed}    ", end="", flush=True)
                    
                    if time.time() - last_update > 1.0:
                        h, m, s = (int(x) for x in t.split(":"))
                        elapsed_sec = h * 3600 + m * 60 + s
                        percent = None
                        eta_sec = None
                        est_size_bytes = None
                        if dur and dur > 0:
                            percent = round(min(100.0, elapsed_sec / dur * 100), 1)
                            try:
                                spd = float(speed.rstrip("x"))
                                if spd > 0:
                                    eta_sec = int(max(0, dur - elapsed_sec) / spd)
                            except ValueError:
                                pass
                            # Arvioidaan lopputiedoston koko ffmpegin size-kentästä (ei ennen 3 % edistymistä)
                            size_match = re.search(r"size=\s*(\d+)\s*[kK]i?B", line)
                            if size_match and percent >= 3:
                                est_size_bytes = int(int(size_match.group(1)) * 1024 / (percent / 100))
                        update_job_progress(record.id, {
                            "file": record.file_path, "time": t, "fps": fps, "speed": speed,
                            "percent": percent, "eta_sec": eta_sec,
                            "est_size_bytes": est_size_bytes, **job_info
                        })
                        last_update = time.time()
        
        proc.wait()
        print("\n")
            
        if proc.returncode != 0:
            error_msg = "\n".join(error_log)
            print(f"\n[FFMPEG VIRHE]\n{error_msg}\n")
            raise EncoderError(f"FFmpeg kaatui. Katso konsolista tarkka syy.", error_log=error_msg)
            
        new_dur = get_duration(temp_path)
        if abs(dur - new_dur) > config.safety.max_duration_diff_sec:
            raise JobManagerError(f"Kestovirhe: {dur}s vs {new_dur}s. Raja: {config.safety.max_duration_diff_sec}s.")
            
        new_size = temp_path.stat().st_size
        saved_pct = (1 - (new_size / record.orig_size_bytes)) * 100
        
        if saved_pct < config.video.min_compression_pct:
            if config.safety.keep_failed_videos:
                failed_path = final_output_path.with_name(final_output_path.name + ".FAILED.mkv")
                atomic_move(temp_path, failed_path)
            else:
                temp_path.unlink(missing_ok=True)
            record.status = JobStatus.SKIPPED
            record.notes = f"Säästö liian pieni ({saved_pct:.1f}%)"
            session.commit()
            if config.video.remux_on_skip:
                ext = config.video.remux_container.lstrip(".")
                remux_out = folder.output_dir / Path(rel_path_str).with_suffix(f".{ext}")
                print(f"  -> remux_on_skip=True: remuxataan {input_path.suffix} → .{ext} (stream-copy)...")
                if _remux_stream_copy(input_path, remux_out):
                    print(f"  -> Remux valmis: {remux_out}")
                    remux_size = remux_out.stat().st_size if remux_out.exists() else 0
                    record.status = JobStatus.COMPLETED
                    record.encoder_used = f"remux_{ext}"
                    record.new_size_bytes = remux_size
                    record.encode_bytes_written = (record.encode_bytes_written or 0) + remux_size
                    record.notes = f"Säästö liian pieni ({saved_pct:.1f}%) | Remuxattu .{ext}"
                    session.commit()
                    add_io_stats(encode_disk_bytes=remux_size)
                else:
                    print(f"  -> Remux epäonnistui.")
            return
            
        if config.files.in_place and input_path != final_output_path:
            input_path.unlink(missing_ok=True)
            
        atomic_move(temp_path, final_output_path)
        
        record.new_size_bytes = new_size
        record.encoder_used = config.video.encoder
        record.processing_time_sec = time.time() - start_time
        record.worker_node = "Palvelin"
        record.status = JobStatus.COMPLETED
        # Säilytetään VMAF-huomio, jos se on olemassa
        if not record.notes:
            record.notes = "OK"
        session.commit()
        
    except Exception as e:
        # Jos käyttäjä pysäytti työn, status on jo asetettu QUEUED:ksi sisäisessä logiikassa.
        # Ulompi except ei saa ylikirjoittaa sitä FAILED:ksi.
        user_stopped = str(e).startswith("Käyttäjä pysäytti")
        if temp_path.exists():
            p_bytes = temp_path.stat().st_size
            if p_bytes > 0 and not user_stopped:
                add_io_stats(encode_disk_bytes=p_bytes)
            if config.safety.keep_failed_videos and not user_stopped:
                failed_path = final_output_path.with_name(final_output_path.name + ".FAILED.mkv")
                atomic_move(temp_path, failed_path)
            elif temp_path.exists():
                temp_path.unlink(missing_ok=True)
        if not user_stopped:
            record.status = JobStatus.FAILED
            if record.notes and ("ICQ:" in record.notes or "CRF:" in record.notes):
                record.notes = f"{record.notes.split(' | Virhe:')[0]} | Virhe: {str(e)}"
            else:
                record.notes = str(e)
            if hasattr(e, 'error_log') and e.error_log:
                record.error_log = e.error_log
            session.commit()
            print(f"[VIRHE] {record.file_path}: {e}")
        else:
            print(f"[STOP] {record.file_path}: Käyttäjä pysäytti, status: {record.status.value}")
        raise
    finally:
        clear_job_progress(record.id)

def _execute_job_in_thread(job_id: int):
    """Suorittaa yhden työn omassa säikeessään ja omalla tietokantasessiolla."""
    session = SessionLocal()
    try:
        config = load_config()
        record = session.query(VideoRecord).filter(VideoRecord.id == job_id).first()
        if not record:
            return True
        process_single_job(record, session, config)
        return True
    except Exception as e:
        print(f"[WORKER SÄIE VIRHE] Työ #{job_id}: {e}")
        return False
    finally:
        session.close()

def run_worker():
    """Pääsilmukka. Lataa config.json uudelleen ennen töiden aloitusta ja
    ajaa samanaikaisia töitä ThreadPoolExecutorilla (max_concurrent_jobs)."""
    consecutive_errors = 0
    config = load_config()

    try:
        cleanup_orphaned_temp(config)
    except Exception as e:
        print(f"[VAROITUS] Väliaikaistiedostojen siivous epäonnistui: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Pidetään kirjaa aktiivisista säietehtävistä: {job_id: future}
    active_futures = {}

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            while True:
                # 1. Siivotaan valmistuneet työt ja tarkistetaan virheet
                done_jobs = [jid for jid, fut in active_futures.items() if fut.done()]
                for jid in done_jobs:
                    fut = active_futures.pop(jid)
                    try:
                        success = fut.result()
                        if success:
                            consecutive_errors = 0
                        else:
                            consecutive_errors += 1
                    except Exception:
                        consecutive_errors += 1

                # 2. Ladataan uusin konfiguraatio
                try:
                    config = load_config()
                except Exception as e:
                    print(f"[VAROITUS] config.json lataus epäonnistui: {e}")

                max_jobs = max(1, getattr(config.video, 'max_concurrent_jobs', 1))

                # Päivitetään palvelimen node-tila
                try:
                    from core.nodes import register_or_update_node
                    register_or_update_node("server", {
                        "node_name": "Palvelin (Master)",
                        "encoder": config.video.encoder,
                        "speedpreset": getattr(config.video, 'encoder_speedpreset', None),
                        "status": "encoding" if active_futures else "idle",
                        "current_job_id": next(iter(active_futures.keys())) if active_futures else None,
                    })
                except Exception:
                    pass

                # 3. Turvatarkistukset
                if not check_disk_space(config.files.temp_dir, config.safety.min_free_space_gb):
                    if not active_futures:
                        print(f"[VAROITUS] Levytila alle {config.safety.min_free_space_gb} GB. Pysäytetään jono.")
                        break

                if consecutive_errors >= config.safety.max_consecutive_errors:
                    if not active_futures:
                        print(f"[VAROITUS] Liian monta peräkkäistä virhettä ({consecutive_errors}). Pysäytetään jono.")
                        break

                # 4. Tarkistetaan kontrollitila
                ctrl = _read_control_state()
                if ctrl in ("stop", "pause"):
                    state_label = "pysäytetty (stop)" if ctrl == "stop" else "tauolla (pause)"
                    print(f"[CTRL] Jono {state_label} — odotetaan resume-signaalia...")
                    while True:
                        time.sleep(2)
                        ctrl = _read_control_state()
                        if ctrl == "run":
                            print("[CTRL] Resume — jono jatkuu.")
                            break

                # 5. Jos vapaita slotteja on, poimitaan seuraavat työt jonosta
                available_slots = max_jobs - len(active_futures)
                if available_slots > 0:
                    with SessionLocal() as session:
                        next_jobs = (
                            session.query(VideoRecord)
                            .filter(VideoRecord.status == JobStatus.QUEUED)
                            .order_by(VideoRecord.created_at.asc())
                            .limit(available_slots)
                            .all()
                        )

                        for job in next_jobs:
                            job.status = JobStatus.PROCESSING
                            job.worker_node = "Palvelin"
                            session.commit()

                            print(f"[JONO] Käynnistetään työ #{job.id} ({len(active_futures)+1}/{max_jobs} samanaikaisesti): {job.file_path}")
                            q_label = "ICQ" if "vaapi" in config.video.encoder else "CRF"
                            print(f"  {q_label} 4K: {config.video.vmaf_icq_candidates_4k} | 1080p: {config.video.vmaf_icq_candidates_1080p} | SD: {config.video.vmaf_icq_candidates_sd} | Tavoite-VMAF: {config.video.target_vmaf}")

                            future = executor.submit(_execute_job_in_thread, job.id)
                            active_futures[job.id] = future

                # Odotetaan pieni hetki ennen seuraavaa tarkistuskierrosta
                time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nKeskeytetty.")

if __name__ == "__main__":
    from core.database import init_db

    print("Alustetaan tietokanta...")
    try:
        init_db()
        print("Käynnistetään Worker...")
        run_worker()
    except Exception as e:
        print(f"Workerin käynnistys epäonnistui: {e}")
        import traceback
        traceback.print_exc()
        try:
            Path("data").mkdir(parents=True, exist_ok=True)
            with open("data/crash.log", "a", encoding="utf-8") as f:
                f.write("\n--- Worker crash ---\n" + traceback.format_exc())
        except Exception:
            pass