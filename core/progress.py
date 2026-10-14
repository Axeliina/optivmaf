import json
import os
import tempfile
import fcntl
from pathlib import Path

_PROGRESS_FILE = Path(os.environ.get("OPTIVMAF_PROGRESS_FILE", "data/progress.json"))
_LOCK_FILE = Path(os.environ.get("OPTIVMAF_PROGRESS_LOCK", "data/progress.json.lock"))

def update_job_progress(job_id: str | int, job_data: dict):
    """
    Päivittää yksittäisen työn tilan monityö-progress.jsoniin turvallisesti tiedostolukolla ja atomisella siirrolla.
    Tallentaa rakenteen:
    {
       "jobs": {
           "<job_id>": { "file": ..., "percent": ..., "fps": ..., ... }
       },
       # Taaksepäinyhteensopivuuskentät (ensisijaisen aktiivisen työn tiedot):
       "file": ...,
       "percent": ...,
       ...
    }
    """
    try:
        _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOCK_FILE, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                current: dict = {}
                if _PROGRESS_FILE.exists():
                    try:
                        with open(_PROGRESS_FILE, "r") as f:
                            current = json.load(f)
                    except Exception:
                        current = {}

                if not isinstance(current, dict):
                    current = {}

                jobs = current.get("jobs", {})
                if not isinstance(jobs, dict):
                    jobs = {}

                str_id = str(job_id)
                # Jos työn data on tyhjä, poistetaan se
                if not job_data:
                    jobs.pop(str_id, None)
                else:
                    jobs[str_id] = {**job_data, "job_id": job_id}

                payload: dict = {"jobs": jobs}

                # Jos on aktiivisia töitä, täytetään myös juuritason kentät taaksepäinyhteensopivuutta varten
                if jobs:
                    primary_job = jobs.get(str_id) or next(iter(jobs.values()))
                    for k, v in primary_job.items():
                        if k not in payload:
                            payload[k] = v

                # Kirjoitetaan atomisesti
                tmp_fd, tmp_path = tempfile.mkstemp(dir=str(_PROGRESS_FILE.parent), prefix="prog_tmp_")
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(payload, f)
                os.replace(tmp_path, str(_PROGRESS_FILE))
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except Exception:
        pass

def clear_job_progress(job_id: str | int):
    """Poistaa annetun työn tilan progress.jsonista."""
    update_job_progress(job_id, {})

def clear_all_progress():
    """Tyhjentää koko progress.jsonin."""
    try:
        _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOCK_FILE, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(dir=str(_PROGRESS_FILE.parent), prefix="prog_tmp_")
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump({}, f)
                os.replace(tmp_path, str(_PROGRESS_FILE))
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except Exception:
        pass

def read_all_progress() -> dict:
    """Lukee progress.jsonin turvallisesti."""
    if _PROGRESS_FILE.exists():
        try:
            with open(_PROGRESS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}
