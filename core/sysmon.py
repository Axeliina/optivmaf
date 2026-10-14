"""Reaaliaikainen seuranta: vain tämän softan (kontin) CPU, RAM, levy-I/O ja GPU.

Mittaus perustuu kontin cgroupiin (kuuluu kaikki kontin prosessit, myös
lyhytikäiset ffmpeg-prosessit). Jos cgroup-tiedostoja ei ole saatavilla,
summataan kontin prosessien (psutil) lukemat. Kontin PID-nimiavaruus näkee
vain kontin omat prosessit, joten isännän muu kuorma ei näy luvuissa.
"""
import os
import shutil
import threading
import time
from pathlib import Path

import psutil

_CG = Path("/sys/fs/cgroup")
_lock = threading.Lock()
_prev = {"ts": None, "cpu_usec": None, "io_read": None, "io_write": None, "proc_cpu": {}}


# ── cgroup-apufunktiot ────────────────────────────────────────────────────────
def _read_kv(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                out[parts[0]] = int(parts[1])
    except Exception:
        pass
    return out


def _cgroup_cpu_usec():
    return _read_kv(_CG / "cpu.stat").get("usage_usec")


def _cgroup_io_bytes():
    """Summaa kontin lukemat/kirjoitukset kaikilta laitteilta (cgroup v2 io.stat)."""
    try:
        text = (_CG / "io.stat").read_text()
    except Exception:
        return None
    r = w = 0
    found = False
    for line in text.splitlines():
        for tok in line.split()[1:]:
            if tok.startswith("rbytes="):
                r += int(tok[7:]); found = True
            elif tok.startswith("wbytes="):
                w += int(tok[7:]); found = True
    return (r, w) if found else None


def _cgroup_mem():
    """Kontin muisti kuten `docker stats`: current - inactive_file. Sisältää /dev/shm-käytön."""
    try:
        cur = int((_CG / "memory.current").read_text().strip())
    except Exception:
        return None
    inactive = _read_kv(_CG / "memory.stat").get("inactive_file", 0)
    used = max(cur - inactive, 0)
    total = None
    try:
        raw = (_CG / "memory.max").read_text().strip()
        if raw != "max":
            total = int(raw)
    except Exception:
        pass
    return used, total


def _cpu_limit_cores():
    """cpu.max-rajoitus ytimissä, tai kaikki ytimet jos ei rajoitusta."""
    n = psutil.cpu_count(logical=True) or 1
    try:
        quota, period = (_CG / "cpu.max").read_text().split()
        if quota != "max":
            return min(n, int(quota) / int(period))
    except Exception:
        pass
    return n


# ── GPU (DRM fdinfo Intel/AMD + NVIDIA nvidia-smi) ──────────────────────────
# Kernel kertoo jokaisen GPU:ta käyttävän prosessin kuorman tiedostossa
# /proc/<pid>/fdinfo/<fd>. Kontin PID-nimiavaruus näkee vain kontin omat
# prosessit, joten luku on automaattisesti vain tämän softan GPU-kuorma.
# Xe:     drm-cycles-<engine> / drm-total-cycles-<engine>   (sykliä)
# i915:   drm-engine-<engine> (nanosekuntia)
# amdgpu: drm-engine-<engine> (nanosekuntia)
_XE_ENGINES = {"rcs": "render", "ccs": "render", "bcs": "copy", "vcs": "video", "vecs": "video_enhance"}
_I915_ENGINES = {"render": "render", "copy": "copy", "video": "video", "video-enhance": "video_enhance"}
_AMDGPU_ENGINES = {"gfx": "render", "compute": "render", "dma": "copy", "dec": "video", "enc": "video", "vce": "video", "uvd": "video", "vcn_dec": "video", "vcn_enc": "video"}
_gpu_prev = {"ts": None, "clients": {}}
_gpu_lock = threading.Lock()


def _scan_drm_clients() -> dict:
    """Palauttaa {(pid, driver, client_id): {engine_group: (busy, total)}} kontin prosesseille."""
    clients = {}
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:
        return clients
    for pid in pids:
        fd_dir = f"/proc/{pid}/fdinfo"
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue
        for fd in fds:
            try:
                with open(f"{fd_dir}/{fd}") as f:
                    text = f.read()
            except Exception:
                continue
            if "drm-driver:" not in text:
                continue
            info = {}
            for line in text.splitlines():
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
            driver = info.get("drm-driver", "")
            cid = info.get("drm-client-id")
            if driver not in ("xe", "i915", "amdgpu") or cid is None:
                continue
            key = (pid, driver, cid)
            if key in clients:  # sama client monen fd:n kautta
                continue
            groups = {}
            if driver == "xe":
                for eng, grp in _XE_ENGINES.items():
                    try:
                        busy = int(info.get(f"drm-cycles-{eng}", "0").split()[0])
                        total = int(info.get(f"drm-total-cycles-{eng}", "0").split()[0])
                        cap = int(info.get(f"drm-engine-capacity-{eng}", "1").split()[0]) or 1
                        busy = busy / cap  # useita instansseja: 100 % = kaikki täynnä
                    except Exception:
                        continue
                    b, t = groups.get(grp, (0, 0))
                    groups[grp] = (b + busy, max(t, total))
            elif driver == "amdgpu":
                for eng, grp in _AMDGPU_ENGINES.items():
                    try:
                        busy = int(info.get(f"drm-engine-{eng}", "0").split()[0])
                    except Exception:
                        continue
                    b, _t = groups.get(grp, (0, 0))
                    groups[grp] = (b + busy, 0)
            else:  # i915: nanosekunteja, total = kellonaika
                for eng, grp in _I915_ENGINES.items():
                    try:
                        busy = int(info.get(f"drm-engine-{eng}", "0").split()[0])
                    except Exception:
                        continue
                    b, _t = groups.get(grp, (0, 0))
                    groups[grp] = (b + busy, 0)
            clients[key] = {"driver": driver, "groups": groups}
    return clients


def _gpu_freq_mhz():
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        # Intel Xe / i915 polut
        for name in ("gt/gt0/rps_cur_freq_mhz", "gt_cur_freq_mhz", "device/tile0/gt0/freq0/cur_freq"):
            p = card / name
            if p.exists():
                try:
                    return int(p.read_text().strip())
                except Exception:
                    pass
        # AMD amdgpu sclk polku (device/pp_dpm_sclk)
        amd_sclk = card / "device/pp_dpm_sclk"
        if amd_sclk.exists():
            try:
                for line in amd_sclk.read_text().splitlines():
                    if "*" in line:  # aktiivinen taajuus, esim "1: 1800Mhz *"
                        parts = line.replace("*", "").split()
                        for part in parts:
                            if "mhz" in part.lower():
                                return int(part.lower().replace("mhz", ""))
            except Exception:
                pass
    return None


def _nvidia_stats() -> dict | None:
    """Tarkistaa onko NVIDIA GPU käytössä nvidia-smi:n kautta."""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder,clocks.current.graphics,power.draw",
             "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
            util_gpu = float(parts[0]) if len(parts) > 0 and parts[0] else 0.0
            util_enc = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
            clock = float(parts[2]) if len(parts) > 2 and parts[2] else None
            power = float(parts[3]) if len(parts) > 3 and parts[3] else None
            return {
                "available": True,
                "scope": "system",
                "render_percent": util_gpu,
                "video_percent": util_enc,
                "video_enhance_percent": 0.0,
                "freq_mhz": clock,
                "power_w": power,
                "idle": (util_gpu == 0 and util_enc == 0),
            }
    except Exception:
        pass
    return None


def get_gpu_stats() -> dict:
    # 1. Kokeile ensin NVIDIA
    nv = _nvidia_stats()
    if nv:
        return nv

    # 2. Intel / AMD DRM fdinfo
    now = time.time()
    cur = _scan_drm_clients()
    with _gpu_lock:
        prev_ts = _gpu_prev["ts"]
        prev = _gpu_prev["clients"]
        _gpu_prev.update(ts=now, clients=cur)

    freq = _gpu_freq_mhz()
    if prev_ts is None:
        return {"available": True, "scope": "process", "render_percent": 0.0, "video_percent": 0.0,
                "video_enhance_percent": 0.0, "freq_mhz": freq, "warming_up": True}
    dt = now - prev_ts
    if dt <= 0:
        dt = 1e-6

    sums = {"render": 0.0, "copy": 0.0, "video": 0.0, "video_enhance": 0.0}
    totals = {}
    saw_client = bool(cur)
    for key, c in cur.items():
        p = prev.get(key)
        if not p:
            continue
        for grp, (busy, total) in c["groups"].items():
            pbusy, ptotal = p["groups"].get(grp, (0, 0))
            db = busy - pbusy
            if db < 0:
                continue
            if c["driver"] == "xe":
                sums[grp] += db
                totals[grp] = max(totals.get(grp, 0), total - ptotal)
            else:  # i915 ja amdgpu (nanosekunteina)
                sums[grp] += db / (dt * 1e9) * 100  # ns -> %

    def pct(grp):
        if grp in totals:  # Xe: syklit
            t = totals[grp]
            return round(min(sums[grp] / t * 100, 100), 1) if t > 0 else 0.0
        return round(min(sums[grp], 100), 1)

    if not saw_client and freq is None:
        return {"available": False,
                "error": "Ei aktiivista GPU:ta tai DRM-clientia (CPU-tila tai /dev/dri puuttuu)"}
    return {
        "available": True,
        "scope": "process",
        "render_percent": pct("render"),
        "video_percent": pct("video"),
        "video_enhance_percent": pct("video_enhance"),
        "freq_mhz": freq,
        "idle": not saw_client,
    }

# ── Levytila ──────────────────────────────────────────────────────────────────
def _disk_usage_for(paths):
    result = []
    seen = set()
    for p in paths:
        if not p:
            continue
        p = str(p)
        try:
            du = shutil.disk_usage(p)
        except Exception:
            continue
        key = (du.total, du.used)
        if key in seen:  # sama levy useammalla polulla
            continue
        seen.add(key)
        result.append({
            "path": p,
            "total_gb": round(du.total / 1024**3, 1),
            "used_gb": round(du.used / 1024**3, 1),
            "free_gb": round(du.free / 1024**3, 1),
            "percent": round(du.used / du.total * 100, 1) if du.total else 0,
        })
    return result


# ── Prosessisumma (fallback jos cgroup ei käytössä) ──────────────────────────
def _proc_sums():
    cpu = rss = r = w = 0.0
    for p in psutil.process_iter():
        try:
            cpu += p.cpu_percent(interval=None)
            rss += p.memory_info().rss
            try:
                io = p.io_counters()
                r += io.read_bytes
                w += io.write_bytes
            except Exception:
                pass
        except Exception:
            continue
    return cpu, rss, r, w


def get_system_stats(disk_paths=None) -> dict:
    now = time.time()
    n_cores = psutil.cpu_count(logical=True) or 1
    limit_cores = _cpu_limit_cores()

    cg_cpu = _cgroup_cpu_usec()
    cg_io = _cgroup_io_bytes()
    proc_cpu, proc_rss, proc_r, proc_w = _proc_sums()

    with _lock:
        prev = dict(_prev)
        dt = (now - prev["ts"]) if prev["ts"] else None

        # CPU: prosentti kontille käytettävissä olevasta CPU-kapasiteetista
        if cg_cpu is not None and prev["cpu_usec"] is not None and dt and dt > 0:
            cpu_pct = (cg_cpu - prev["cpu_usec"]) / (dt * 1e6) / limit_cores * 100
        elif cg_cpu is None:
            cpu_pct = proc_cpu / limit_cores
        else:
            cpu_pct = 0.0

        # Levy-I/O
        if cg_io is not None:
            r_now, w_now = cg_io
        else:
            r_now, w_now = proc_r, proc_w
        if prev["io_read"] is not None and dt and dt > 0:
            io = {
                "read_mb_s": round(max(r_now - prev["io_read"], 0) / dt / 1024**2, 1),
                "write_mb_s": round(max(w_now - prev["io_write"], 0) / dt / 1024**2, 1),
            }
        else:
            io = {"read_mb_s": 0.0, "write_mb_s": 0.0}

        _prev.update(ts=now, cpu_usec=cg_cpu, io_read=r_now, io_write=w_now)

    # RAM
    mem = _cgroup_mem()
    host_total = psutil.virtual_memory().total
    if mem:
        used, limit = mem
    else:
        used, limit = proc_rss, None
    total = limit if limit and limit < host_total else host_total

    return {
        "scope": "container",
        "cpu": {
            "percent": round(max(0.0, min(cpu_pct, 100.0)), 1),
            "cores": round(limit_cores, 1) if limit_cores != n_cores else n_cores,
        },
        "ram": {
            "percent": round(used / total * 100, 1) if total else 0,
            "used_gb": round(used / 1024**3, 2),
            "total_gb": round(total / 1024**3, 1),
        },
        "disk": {
            "volumes": _disk_usage_for(disk_paths or ["/"]),
            "io": io,
        },
        "gpu": get_gpu_stats(),
    }
