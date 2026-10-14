import time
import threading
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from core.database import VideoRecord, JobStatus, SessionLocal
from core.progress import clear_job_progress

_lock = threading.Lock()
_nodes: Dict[str, Dict[str, Any]] = {}

def register_or_update_node(node_id: str, data: dict, client_ip: str = "") -> dict:
    """Rekisteröi tai päivittää työaseman (noden) tilan."""
    now = time.time()
    with _lock:
        if node_id not in _nodes:
            _nodes[node_id] = {
                "node_id": node_id,
                "node_name": data.get("node_name", node_id),
                "hostname": data.get("hostname", ""),
                "ip": client_ip or data.get("ip", ""),
                "encoder": data.get("encoder", "unknown"),
                "speedpreset": data.get("speedpreset"),
                "status": data.get("status", "idle"),
                "current_job_id": data.get("current_job_id"),
                "connected_at": now,
                "last_heartbeat": now,
                "total_jobs_completed": 0,
                "system_info": data.get("system_info", {}),
            }
        else:
            node = _nodes[node_id]
            node["last_heartbeat"] = now
            if "node_name" in data:
                node["node_name"] = data["node_name"]
            if "status" in data:
                node["status"] = data["status"]
            if "current_job_id" in data:
                node["current_job_id"] = data["current_job_id"]
            if "encoder" in data:
                node["encoder"] = data["encoder"]
            if "speedpreset" in data:
                node["speedpreset"] = data["speedpreset"]
            if "system_info" in data:
                node["system_info"] = data["system_info"]
            if client_ip:
                node["ip"] = client_ip

        return dict(_nodes[node_id])

def update_node_job(node_id: str, job_id: Optional[int], status: str = "encoding"):
    """Päivittää noden parhaillaan työn alla olevan työn."""
    with _lock:
        if node_id in _nodes:
            _nodes[node_id]["current_job_id"] = job_id
            _nodes[node_id]["status"] = status
            _nodes[node_id]["last_heartbeat"] = time.time()

def record_node_completed(node_id: str):
    """Kasvattaa noden valmistuneiden töiden määrää ja vapauttaa tilan."""
    with _lock:
        if node_id in _nodes:
            _nodes[node_id]["total_jobs_completed"] = _nodes[node_id].get("total_jobs_completed", 0) + 1
            _nodes[node_id]["current_job_id"] = None
            _nodes[node_id]["status"] = "idle"
            _nodes[node_id]["last_heartbeat"] = time.time()

def get_node(node_id: str) -> Optional[dict]:
    with _lock:
        node = _nodes.get(node_id)
        if node:
            res = dict(node)
            res["is_online"] = (time.time() - res["last_heartbeat"]) < 45.0
            return res
        return None

def get_all_nodes() -> List[dict]:
    """Palauttaa kaikki rekisteröidyt nodet ja niiden tilat."""
    now = time.time()
    result = []
    with _lock:
        for nid, data in _nodes.items():
            item = dict(data)
            item["is_online"] = (now - item["last_heartbeat"]) < 45.0
            item["seconds_since_heartbeat"] = round(now - item["last_heartbeat"], 1)
            result.append(item)
    return result

def remove_node(node_id: str):
    with _lock:
        _nodes.pop(node_id, None)

def recover_stale_nodes(timeout_sec: float = 60.0):
    """
    Tarkistaa onko jokin ulkoinen node katkennut (ei heartbeatia yli timeout_sec sekuntiin)
    kesken työn suorituksen. Jos on, palauttaa työn automaattisesti takaisin QUEUED-tilaan.
    """
    now = time.time()
    stuck_jobs: List[tuple[str, int]] = []

    with _lock:
        for nid, node in _nodes.items():
            if nid == "server" or nid == "local":
                continue # Paikallinen palvelin hoitaa omat kaatumisensa job_managerissa
            time_since = now - node.get("last_heartbeat", 0)
            cur_job = node.get("current_job_id")
            if time_since > timeout_sec and cur_job is not None:
                stuck_jobs.append((nid, cur_job))
                node["current_job_id"] = None
                node["status"] = "offline"

    if stuck_jobs:
        with SessionLocal() as db:
            for node_id, job_id in stuck_jobs:
                job = db.query(VideoRecord).filter(VideoRecord.id == job_id).first()
                if job and job.status == JobStatus.PROCESSING:
                    print(f"[NODES] Node '{node_id}' yhteys katkesi! Palautetaan työ #{job_id} ({job.file_path}) takaisin jonoon.")
                    job.status = JobStatus.QUEUED
                    job.notes = (job.notes or "") + f" | Palautettu jonoon (Node '{node_id}' offline)"
                    job.worker_node = None
                    db.commit()
                clear_job_progress(job_id)
