import json
import os
import threading
from pathlib import Path

STATS_FILE = Path("data/disk_stats.json")
_stats_lock = threading.Lock()

def get_io_stats() -> dict:
    default_stats = {
        "encode_disk_bytes": 0,
        "vmaf_disk_bytes": 0,
        "vmaf_ram_bytes": 0
    }
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "encode_disk_bytes": int(data.get("encode_disk_bytes", 0)),
                    "vmaf_disk_bytes": int(data.get("vmaf_disk_bytes", 0)),
                    "vmaf_ram_bytes": int(data.get("vmaf_ram_bytes", 0))
                }
        except Exception:
            pass
    return default_stats

def add_io_stats(encode_disk_bytes: int = 0, vmaf_disk_bytes: int = 0, vmaf_ram_bytes: int = 0):
    with _stats_lock:
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            stats = get_io_stats()
            stats["encode_disk_bytes"] += max(0, int(encode_disk_bytes))
            stats["vmaf_disk_bytes"] += max(0, int(vmaf_disk_bytes))
            stats["vmaf_ram_bytes"] += max(0, int(vmaf_ram_bytes))
            
            tmp_file = STATS_FILE.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
            tmp_file.replace(STATS_FILE)
        except Exception as e:
            print(f"[VAROITUS] disk_stats.json päivitys epäonnistui: {e}")

def init_io_stats_from_db(db):
    from core.database import VideoRecord, JobStatus
    try:
        stats = get_io_stats()
        completed = db.query(VideoRecord).filter(VideoRecord.status.in_([JobStatus.COMPLETED, JobStatus.APPLIED])).all()
        db_encode_bytes = sum(getattr(v, "encode_bytes_written", None) or v.new_size_bytes or 0 for v in completed)
        db_vmaf_disk_bytes = sum(getattr(v, "vmaf_bytes_written", 0) or 0 for v in completed if getattr(v, "vmaf_storage_type", "ram") == "disk")
        db_vmaf_ram_bytes = sum(getattr(v, "vmaf_bytes_written", 0) or 0 for v in completed if getattr(v, "vmaf_storage_type", "ram") != "disk")
        
        updated = False
        if stats["encode_disk_bytes"] < db_encode_bytes:
            stats["encode_disk_bytes"] = db_encode_bytes
            updated = True
        if stats["vmaf_disk_bytes"] < db_vmaf_disk_bytes:
            stats["vmaf_disk_bytes"] = db_vmaf_disk_bytes
            updated = True
        if stats["vmaf_ram_bytes"] < db_vmaf_ram_bytes:
            stats["vmaf_ram_bytes"] = db_vmaf_ram_bytes
            updated = True
            
        if updated or not STATS_FILE.exists():
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = STATS_FILE.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
            tmp_file.replace(STATS_FILE)
    except Exception as e:
        print(f"[VAROITUS] init_io_stats_from_db epäonnistui: {e}")

def reset_io_stats():
    with _stats_lock:
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            stats = {
                "encode_disk_bytes": 0,
                "vmaf_disk_bytes": 0,
                "vmaf_ram_bytes": 0
            }
            tmp_file = STATS_FILE.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
            tmp_file.replace(STATS_FILE)
        except Exception as e:
            print(f"[VAROITUS] disk_stats.json nollaus epäonnistui: {e}")

