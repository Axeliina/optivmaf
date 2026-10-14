#!/usr/bin/env python3
"""
Optivmaf Hajautettu Worker-agentti (Node)
-----------------------------------------
Tämä skripti voidaan ajaa missä tahansa lähiverkon koneessa (esim. Windows-peli-PC tai toinen Linux-kone).
Se pyytää töitä Optivmaf-palvelimelta, koodaa videot paikallisesti (CPU libsvtav1 tai GPU NVENC/AMF)
ja palauttaa valmiit videot verkkolevylle.

Ei vaadi ulkoisia pip-kirjastoja — toimii pelkällä Python 3.10+ standardikirjastolla ja FFmpegillä!
"""

import os
import sys
import time
import json
import shutil
import signal
import socket
import tempfile
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

CONFIG_FILE = Path("node_config.json")
EXAMPLE_CONFIG_FILE = Path("node_config.example.json")

DEFAULT_CONFIG = {
    "master_url": "http://192.168.1.100:6699",
    "node_id": f"worker-{socket.gethostname().lower()}",
    "node_name": f"Peli-PC ({socket.gethostname()})",
    "encoder": "libsvtav1",
    "encoder_speedpreset": None,
    "preset_svtav1": 6,
    "preset_nvenc": 5,
    "preset_vaapi": 1,
    "preset_cpu_fallback": 5,
    "film_grain": 8,
    "film_grain_denoise": False,
    "force_10bit": True,
    "temp_dir": "",
    "path_mapping": {
        "/mnt/dell": "Z:\\dell",
        "/mnt/storage": "Z:\\storage"
    },
    "poll_interval_sec": 4,
    "skip_vmaf": False,
    "fixed_icq": 24
}

class WorkerStopException(Exception):
    pass

class RemoteWorker:
    def __init__(self, config_path: Path = CONFIG_FILE):
        self.config = self._load_config(config_path)
        self.master_url = self.config["master_url"].rstrip("/")
        self.node_id = self.config["node_id"]
        self.node_name = self.config["node_name"]
        self.encoder = self.config.get("encoder", "libsvtav1")
        self.speedpreset = self.get_speedpreset()
        self.path_mapping = self.config.get("path_mapping", {})
        self.poll_interval = self.config.get("poll_interval_sec", 4)
        
        # Temp-hakemisto paikalliselle levylle
        raw_temp = self.config.get("temp_dir")
        if raw_temp:
            self.temp_dir = Path(raw_temp)
        else:
            self.temp_dir = Path(tempfile.gettempdir()) / "optivmaf_worker"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self.current_job_id: Optional[int] = None
        self.current_proc: Optional[subprocess.Popen] = None
        self.is_running = True
        self.is_paused = False

        self._check_ffmpeg()

    def _load_config(self, path: Path) -> dict:
        if not path.exists():
            if EXAMPLE_CONFIG_FILE.exists():
                print(f"[INFO] Luodaan {path} esimerkkikonfiguraatiosta...")
                shutil.copy(EXAMPLE_CONFIG_FILE, path)
            else:
                print(f"[INFO] Luodaan oletuskonfiguraatio: {path}")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_CONFIG, f, indent=4)
        
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Täytetään puuttuvat oletusarvoilla
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
        except Exception as e:
            print(f"[VIRHE] Konfiguraation lukeminen epäonnistui: {e}")
            return DEFAULT_CONFIG

    def _check_ffmpeg(self):
        """Varmistaa että ffmpeg ja ffprobe löytyvät järjestelmästä ja tarkistaa VMAF-tuen."""
        for tool in ("ffmpeg", "ffprobe"):
            try:
                res = subprocess.run([tool, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode != 0:
                    print(f"[VAROITUS] '{tool}' palautti virhekoodin.")
            except FileNotFoundError:
                print(f"\n[KRIITTINEN VIRHE] '{tool}' ei löydy PATH-ympäristömuuttujasta!")
                print(f"Asenna FFmpeg ja lisää se järjestelmän PATH-polkuun (esim. winget install Gyan.FFmpeg Windowsissa).\n")
                sys.exit(1)

        # Tarkistetaan libvmaf-tuki: tutkitaan sekä ffmpeg-vmaf että ffmpeg
        self.vmaf_bin = None
        self.has_vmaf = False
        for candidate in ("ffmpeg-vmaf", "ffmpeg"):
            if shutil.which(candidate):
                try:
                    res = subprocess.run([candidate, "-filters"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    if "libvmaf" in res.stdout:
                        self.vmaf_bin = candidate
                        self.has_vmaf = True
                        break
                except Exception:
                    pass

        vmaf_status = f"Kyllä ({self.vmaf_bin} + libvmaf)" if self.has_vmaf else "Ei (käytetään kiinteää CRF/CQ-arvoa)"
        print(f"[FFMPEG] Enkooderi: {self.encoder} | VMAF-tuki paikallisesti: {vmaf_status}")

    def api_call(self, endpoint: str, data: Optional[dict] = None, method: str = "GET", timeout: int = 15) -> Optional[dict]:
        """Tekee HTTP-kutsun Optivmaf-palvelimelle ilman ulkoisia kirjastoja."""
        url = f"{self.master_url}{endpoint}"
        headers = {"User-Agent": "Optivmaf-Remote-Worker/1.0", "Content-Type": "application/json"}
        body_bytes = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                resp_text = response.read().decode("utf-8")
                if resp_text:
                    return json.loads(resp_text)
                return {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            print(f"[API VIRHE] HTTP {e.code} ({endpoint}): {err_body}")
            return None
        except Exception as e:
            # Yhteysvirhe / timeout
            return None

    def map_path(self, server_path_str: str) -> Path:
        """Kääntää palvelimen Linux-polun peli-PC:n levyksi tai verkkojaoksi."""
        # Etsitään pisin täsmäävä prefix
        matched_prefix = None
        for prefix in sorted(self.path_mapping.keys(), key=len, reverse=True):
            p_clean = prefix.rstrip("/\\")
            if server_path_str == p_clean or server_path_str.startswith(p_clean + "/") or server_path_str.startswith(p_clean + "\\"):
                matched_prefix = prefix
                break

        if matched_prefix:
            rel = server_path_str[len(matched_prefix):].lstrip("/\\")
            target_root = self.path_mapping[matched_prefix]
            return Path(target_root) / rel
        
        return Path(server_path_str)

    def get_speedpreset(self, job_cfg: Optional[dict] = None) -> int:
        """Palauttaa työlle tai workerille käytettävän nopeus/preset-arvon."""
        if self.config.get("encoder_speedpreset") is not None:
            return self.config["encoder_speedpreset"]
        cfg = job_cfg or {}
        enc_lower = self.encoder.lower()
        if "vaapi" in enc_lower:
            return self.config.get("preset_vaapi", cfg.get("preset_vaapi", 1))
        elif "nvenc" in enc_lower:
            return self.config.get("preset_nvenc", cfg.get("preset_nvenc", 5))
        elif "svt" in enc_lower:
            return self.config.get("preset_svtav1", cfg.get("preset_svtav1", 6))
        else:
            return self.config.get("preset_cpu_fallback", cfg.get("preset_cpu_fallback", 5))

    def heartbeat(self) -> str:
        """Lähettää elonmerkin ja palauttaa palvelimen kontrollitilan ('run', 'pause', 'stop')."""
        payload = {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "encoder": self.encoder,
            "speedpreset": self.get_speedpreset(),
            "status": "encoding" if self.current_job_id else ("paused" if self.is_paused else "idle"),
            "current_job_id": self.current_job_id,
            "hostname": socket.gethostname(),
        }
        res = self.api_call("/api/nodes/heartbeat", data=payload, method="POST")
        if res and "control" in res:
            return res["control"]
        return "run"

    def claim_job(self) -> Optional[dict]:
        """Pyytää palvelimelta seuraavan työn."""
        res = self.api_call(f"/api/nodes/{self.node_id}/claim-job", method="POST")
        if res and res.get("job"):
            return res["job"]
        return None

    def report_progress(self, job_id: int, pdata: dict) -> str:
        """Raportoi edistymisen palvelimelle ja palauttaa kontrollitilan."""
        payload = {
            "job_id": job_id,
            "node": self.node_name,
            **pdata
        }
        res = self.api_call(f"/api/nodes/{self.node_id}/progress", data=payload, method="POST")
        if res and "control" in res:
            return res["control"]
        return "run"

    def report_complete(self, job_id: int, cdata: dict):
        """Kuittaa työn valmiiksi."""
        payload = {
            "job_id": job_id,
            **cdata
        }
        self.api_call(f"/api/nodes/{self.node_id}/complete", data=payload, method="POST")

    def report_fail(self, job_id: int, error: str, error_log: Optional[str] = None):
        """Raportoi virheen."""
        payload = {
            "job_id": job_id,
            "error": error,
            "error_log": error_log
        }
        self.api_call(f"/api/nodes/{self.node_id}/fail", data=payload, method="POST")

    def release_job(self, job_id: int, reason: str = "Worker pysäytetty"):
        """Palauttaa työn takaisin jonoon."""
        payload = {
            "job_id": job_id,
            "reason": reason
        }
        self.api_call(f"/api/nodes/{self.node_id}/release", data=payload, method="POST")

    # --- MEDIATIETOJEN PROBE & FFPROBE ---

    def get_duration(self, file_path: Path) -> float:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            try:
                return float(res.stdout.strip())
            except ValueError:
                pass
        return 0.0

    def probe_video(self, file_path: Path):
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", str(file_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffprobe epäonnistui tiedostolle: {file_path}")

        data = json.loads(res.stdout)
        duration = float(data.get("format", {}).get("duration", 0.0))

        vid_idx = None
        vid_codec = "unknown"
        color_space = "unknown"
        height = 0
        audio_tracks = []
        sub_tracks = []

        for s in data.get("streams", []):
            ctype = s.get("codec_type")
            cname = s.get("codec_name", "")
            idx = s.get("index")

            if s.get("disposition", {}).get("attached_pic") == 1 or cname in ["mjpeg", "png", "gif", "bmp"]:
                continue

            lang = s.get("tags", {}).get("language", "und").lower()

            if ctype == "video" and vid_idx is None:
                vid_idx = idx
                vid_codec = cname.lower()
                color_space = s.get("color_space", "unknown").lower()
                height = int(s.get("height", 0))
            elif ctype == "audio":
                audio_tracks.append({
                    "idx": idx, "lang": lang,
                    "channels": int(s.get("channels", 2)),
                    "codec_name": cname.lower()
                })
            elif ctype == "subtitle":
                sub_tracks.append({
                    "idx": idx, "lang": lang,
                    "codec_name": cname.lower()
                })

        return duration, vid_idx, vid_codec, color_space, height, audio_tracks, sub_tracks

    def filter_streams(self, audio_tracks: list, sub_tracks: list, allowed_langs: list, drop_sub_codecs: list = None):
        drop_subs = drop_sub_codecs or []
        keep_a = []
        for a in audio_tracks:
            l = a.get("lang", "und")
            if not allowed_langs or l in allowed_langs or l in ("und", ""):
                keep_a.append(a)
        if not keep_a and audio_tracks:
            keep_a = [audio_tracks[0]]

        keep_s = []
        for s in sub_tracks:
            if s.get("codec_name") in drop_subs:
                continue
            l = s.get("lang", "und")
            if not allowed_langs or l in allowed_langs:
                keep_s.append(s)

        return keep_a, keep_s

    # --- FFMPEG KOMENNON RAKENNUS ---

    def build_ffmpeg_cmd(
        self,
        input_path: Path,
        output_path: Path,
        vid_idx: int,
        height: int,
        audios: list,
        subs: list,
        q_value: int,
        job_cfg: dict
    ) -> list[str]:
        cmd = ["ffmpeg", "-y", "-i", str(input_path), "-map", f"0:{vid_idx}"]

        enc = self.encoder.lower()
        is_svt = "svtav1" in enc
        is_nvenc = "nvenc" in enc
        is_amf = "amf" in enc

        v_filters = []
        if job_cfg.get("deinterlace"):
            v_filters.append("yadif=1")

        max_res = job_cfg.get("max_resolution")
        if max_res and height > max_res:
            v_filters.append(f"scale=-2:{max_res}")

        # Formaatit ja pikseliformaatit
        force_10bit = self.config.get("force_10bit", job_cfg.get("force_10bit", True))
        if is_svt:
            if force_10bit:
                v_filters.append("format=yuv420p10le")
        elif is_nvenc:
            fmt = "p010le" if force_10bit else "yuv420p"
            v_filters.append(f"format={fmt}")
        elif is_amf:
            fmt = "p010le" if force_10bit else "yuv420p"
            v_filters.append(f"format={fmt}")
        else:
            if force_10bit:
                v_filters.append("format=yuv420p10le")

        if v_filters:
            cmd.extend(["-filter:v", ",".join(v_filters)])

        cmd.extend(["-c:v", self.encoder])

        eff_preset = self.get_speedpreset(job_cfg)

        # Enkooderikohtaiset laatu- ja nopeusparametrit
        if is_svt:
            cmd.extend(["-crf", str(q_value)])
            if eff_preset is not None:
                cmd.extend(["-preset", str(eff_preset)])
            svt_params = []
            grain = self.config.get("film_grain", job_cfg.get("film_grain", 0))
            if grain > 0:
                svt_params.append(f"film-grain={grain}")
                if self.config.get("film_grain_denoise", job_cfg.get("film_grain_denoise", False)):
                    svt_params.append("film-grain-denoise=1")
            tile_cols = job_cfg.get("tile_columns", 1)
            tile_rows = job_cfg.get("tile_rows", 1)
            if tile_cols is not None:
                svt_params.append(f"tile-columns={tile_cols}")
            if tile_rows is not None:
                svt_params.append(f"tile-rows={tile_rows}")
            if svt_params:
                cmd.extend(["-svtav1-params", ":".join(svt_params)])
        elif is_nvenc:
            cmd.extend(["-rc", "vbr", "-cq", str(q_value), "-b:v", "0"])
            p_val = f"p{eff_preset}" if isinstance(eff_preset, int) and 1 <= eff_preset <= 7 else "p5"
            cmd.extend(["-preset", p_val])
        elif is_amf:
            cmd.extend(["-rc", "cqp", "-qp_i", str(q_value), "-qp_p", str(q_value)])
        else:
            # CPU x265/x264 tai muut
            cmd.extend(["-crf", str(q_value)])
            if eff_preset is not None:
                cmd.extend(["-preset", str(eff_preset)])

        # Yhteiset videon parametrit
        gop = job_cfg.get("gop_size")
        if gop is not None:
            cmd.extend(["-g", str(gop)])
        b_dep = job_cfg.get("b_depth")
        if b_dep is not None:
            cmd.extend(["-bf", str(b_dep)])
            if job_cfg.get("b_strategy"):
                cmd.extend(["-b_strategy", "1"])

        # Ääniraidat
        passthrough = job_cfg.get("passthrough_codecs", ["truehd", "dts", "eac3"])
        smart_audio = job_cfg.get("smart_audio", False)
        fallback_c = job_cfg.get("fallback_codec", "aac")
        fallback_b = job_cfg.get("fallback_bitrate", "384k")
        add_stereo = job_cfg.get("add_stereo_downmix", False)
        stereo_codec = job_cfg.get("stereo_downmix_codec", "aac")
        stereo_bitrate = job_cfg.get("stereo_downmix_bitrate", "192k")

        out_a_idx = 0
        for a in audios:
            channels = a.get("channels", 2)
            is_stereo = channels <= 2
            is_pass = a["codec_name"] in passthrough

            cmd.extend(["-map", f"0:{a['idx']}"])
            if is_pass:
                cmd.extend([f"-c:a:{out_a_idx}", "copy"])
            elif smart_audio and is_stereo:
                cmd.extend([f"-c:a:{out_a_idx}", fallback_c, f"-b:a:{out_a_idx}", fallback_b])
            else:
                cmd.extend([f"-c:a:{out_a_idx}", "copy"])
            out_a_idx += 1

            # Stereodownmix 2.0 monikanavaäänen rinnalle
            if add_stereo and not is_stereo:
                cmd.extend(["-map", f"0:{a['idx']}"])
                cmd.extend([
                    f"-c:a:{out_a_idx}", stereo_codec,
                    f"-b:a:{out_a_idx}", stereo_bitrate,
                    "-ac", "2"
                ])
                out_a_idx += 1

        # Tekstitykset
        out_s_idx = 0
        for s in subs:
            cmd.extend(["-map", f"0:{s['idx']}"])
            if s.get("codec_name") == "subrip":
                cmd.extend([f"-c:s:{out_s_idx}", "subrip"])
            else:
                cmd.extend([f"-c:s:{out_s_idx}", "copy"])
            out_s_idx += 1

        cmd.append("-dn")

        custom_flags = job_cfg.get("custom_flags", [])
        if custom_flags:
            cmd.extend(custom_flags)

        # Kontaineriasetukset: metatietojen siivous, oletusraidat, kappaleet
        if job_cfg.get("clean_metadata", False):
            cmd.extend(["-map_metadata", "-1"])

        if job_cfg.get("normalize_default_tracks", False):
            for i in range(out_a_idx):
                cmd.extend([f"-disposition:a:{i}", "default" if i == 0 else "0"])
            for i in range(out_s_idx):
                cmd.extend([f"-disposition:s:{i}", "0"])

        if job_cfg.get("keep_chapters", True):
            cmd.extend(["-map_chapters", "0"])

        cmd.append(str(output_path))
        return cmd

    # --- VMAF-HAARUKOINTI ---

    def find_best_icq(
        self,
        input_path: Path,
        vid_idx: int,
        height: int,
        duration: float,
        job_cfg: dict,
        job_data: dict
    ) -> Tuple[int, Optional[float]]:
        """
        Suorittaa VMAF-haarukoinnin ja valitsee optimaalisen ICQ/CRF/CQ-laadun.
        Tukee 1. klipin pikahylkäystä (vmaf_early_reject_margin).
        """
        enc_name = self.encoder.lower()
        is_nvenc = "nvenc" in enc_name
        is_amf = "amf" in enc_name
        is_svt = "svtav1" in enc_name
        q_label = "CQ" if is_nvenc else ("QP" if is_amf else "CRF")

        effective_height = min(height, job_cfg.get("max_resolution")) if (job_cfg.get("max_resolution") and height > 0) else height

        if effective_height >= 1440:
            cands = job_cfg.get("vmaf_icq_candidates_4k", [36, 33, 30, 27, 24])
        elif effective_height >= 1000:
            cands = job_cfg.get("vmaf_icq_candidates_1080p", [30, 27, 24, 21, 18])
        else:
            cands = job_cfg.get("vmaf_icq_candidates_sd", [28, 25, 22, 19, 16])

        fixed_default = self.config.get("fixed_icq", job_cfg.get("fixed_icq", cands[len(cands)//2]))

        if not getattr(self, "has_vmaf", False) or not getattr(self, "vmaf_bin", None):
            print(f"-> [INFO] Ei libvmaf-tukea paikallisessa FFmpegissä, käytetään kiinteää {q_label}-arvoa {fixed_default}.")
            return fixed_default, None

        if self.config.get("skip_vmaf", False) or job_cfg.get("skip_vmaf", False):
            print(f"-> VMAF-tarkistus ohitettu asetuksista, käytetään kiinteää {q_label}-arvoa {fixed_default}.")
            return fixed_default, None


        clip_len = job_cfg.get("vmaf_clip_length_sec", 5)
        if duration < clip_len * 3:
            print(f"-> Video liian lyhyt VMAF-testille ({duration:.1f}s), käytetään {fixed_default}.")
            return fixed_default, None

        target_vmaf = float(job_cfg.get("target_vmaf", 93.0))
        min_vmaf = float(job_cfg.get("min_vmaf", 91.0))
        reject_margin = float(job_cfg.get("vmaf_early_reject_margin", 2.0))
        force_10bit = self.config.get("force_10bit", job_cfg.get("force_10bit", True))

        percentages = [0.20, 0.50, 0.80]
        vmaf_tmp = self.temp_dir / f"vmaf_tmp_{job_data['id']}"
        vmaf_tmp.mkdir(parents=True, exist_ok=True)

        print(f"-> Suoritetaan VMAF-haarukointi ({self.encoder}) | Tavoite: {target_vmaf} (min: {min_vmaf})")
        print(f"   Kandidaatit: {cands} | Klippipituus: {clip_len}s | Pikahylkäys: -{reject_margin:.1f}p")

        lossless_exts = {ext.lower() if ext.startswith('.') else f'.{ext.lower()}' 
                         for ext in job_cfg.get("vmaf_lossless_ref_exts", [".wmv", ".avi"])}
        use_lossless_ref = input_path.suffix.lower() in lossless_exts

        ref_clips = []
        try:
            for i, pct in enumerate(percentages):
                start_t = max(0, duration * pct)
                ref_clip = vmaf_tmp / f"ref_{i}.mkv"
                if use_lossless_ref:
                    cmd_ref = [
                        "ffmpeg", "-y",
                        "-ss", str(start_t),
                        "-i", str(input_path),
                        "-t", str(clip_len),
                        "-map", f"0:{vid_idx}",
                        "-vf", "setpts=PTS-STARTPTS",
                        "-c:v", "ffv1",
                        "-an", "-sn",
                        str(ref_clip)
                    ]
                else:
                    cmd_ref = [
                        "ffmpeg", "-y",
                        "-ss", str(start_t),
                        "-i", str(input_path),
                        "-t", str(clip_len),
                        "-map", f"0:{vid_idx}",
                        "-c:v", "copy",
                        "-an", "-sn",
                        "-avoid_negative_ts", "make_zero",
                        str(ref_clip)
                    ]
                subprocess.run(cmd_ref, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                if ref_clip.exists() and ref_clip.stat().st_size > 0:
                    ref_clips.append(ref_clip)

            if not ref_clips:
                print("   [VAROITUS] Referenssiklippien luonti epäonnistui. Käytetään oletusta.")
                return fixed_default, None

            completed_results = []
            best_icq = cands[0]
            best_vmaf = 0.0
            fallback_icq = None
            fallback_vmaf = 0.0

            for icq in cands:
                ctrl = self.heartbeat()
                if ctrl == "stop":
                    raise WorkerStopException("Käyttäjä pysäytti työn VMAF-testin aikana.")

                self.report_progress(job_data["id"], {
                    "file": job_data["rel_path"],
                    "vmaf_testing": True,
                    "vmaf_current_icq": icq,
                    "vmaf_results": completed_results,
                    "vmaf_candidates": cands,
                    "vmaf_storage": "disk",
                    "encoder": self.encoder,
                    "speed": "VMAF-haarukointi"
                })

                vmaf_results = []
                for i, ref_clip in enumerate(ref_clips):
                    test_clip = vmaf_tmp / f"test_{icq}_{i}.mkv"
                    vmaf_log = vmaf_tmp / f"vmaf_{icq}_{i}.json"

                    cmd_enc = ["ffmpeg", "-y", "-i", str(ref_clip)]
                    test_filters = []
                    if is_svt:
                        if force_10bit: test_filters.append("format=yuv420p10le")
                    elif is_nvenc or is_amf:
                        test_filters.append(f"format={'p010le' if force_10bit else 'yuv420p'}")
                    else:
                        if force_10bit: test_filters.append("format=yuv420p10le")

                    if test_filters:
                        cmd_enc.extend(["-vf", ",".join(test_filters)])

                    cmd_enc.extend(["-c:v", self.encoder])
                    eff_preset = self.get_speedpreset(job_cfg)
                    if is_svt:
                        cmd_enc.extend(["-crf", str(icq)])
                        if eff_preset is not None:
                            cmd_enc.extend(["-preset", str(eff_preset)])
                    elif is_nvenc:
                        cmd_enc.extend(["-rc", "vbr", "-cq", str(icq), "-b:v", "0"])
                        p_val = f"p{eff_preset}" if isinstance(eff_preset, int) and 1 <= eff_preset <= 7 else "p5"
                        cmd_enc.extend(["-preset", p_val])
                    elif is_amf:
                        cmd_enc.extend(["-rc", "cqp", "-qp_i", str(icq), "-qp_p", str(icq)])
                    else:
                        cmd_enc.extend(["-crf", str(icq)])
                        if eff_preset is not None:
                            cmd_enc.extend(["-preset", str(eff_preset)])

                    cmd_enc.extend(["-an", "-sn", str(test_clip)])
                    enc_res = subprocess.run(cmd_enc, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    if enc_res.returncode != 0 or not test_clip.exists():
                        print(f"   [ENC VIRHE] {q_label} {icq} klippi {i}")
                        continue

                    # VMAF-laskenta
                    vmaf_fmt = "yuv420p10le" if force_10bit else "yuv420p"
                    cmd_vmaf = [
                        self.vmaf_bin, "-y",
                        "-i", str(test_clip),
                        "-i", str(ref_clip),
                        "-filter_complex",
                        f"[0:v]format={vmaf_fmt},setpts=N/FRAME_RATE/TB[dist];"
                        f"[1:v]format={vmaf_fmt},setpts=N/FRAME_RATE/TB[ref];"
                        f"[dist][ref]libvmaf=log_fmt=json:log_path={vmaf_log}",
                        "-f", "null", "-"
                    ]
                    vmaf_res = subprocess.run(cmd_vmaf, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    if vmaf_res.returncode != 0 or not vmaf_log.exists():
                        print(f"   [VMAF VIRHE] {q_label} {icq} klippi {i}")
                        continue

                    try:
                        with open(vmaf_log, "r") as f:
                            vmaf_val = json.load(f)["pooled_metrics"]["vmaf"]["mean"]
                        vmaf_results.append(vmaf_val)
                        print(f"   {q_label} {icq} klippi {i}: VMAF={vmaf_val:.2f}")

                        # Älykäs pikahylkäys 1. klipin jälkeen
                        if i == 0 and len(ref_clips) > 1:
                            is_last = (icq == cands[-1])
                            if not is_last and reject_margin > 0:
                                threshold = target_vmaf - reject_margin
                                if vmaf_val < threshold:
                                    print(f"   -> {q_label} {icq} klippi 0 (VMAF {vmaf_val:.2f}) < kynnys {threshold:.1f}. Ohitetaan loput {len(ref_clips)-1} klippiä.")
                                    break
                    except Exception as e:
                        print(f"   [VIRHE] VMAF-tuloksen luku: {e}")

                if not vmaf_results:
                    completed_results.append({"icq": icq, "vmaf": None})
                    continue

                avg_vmaf = sum(vmaf_results) / len(vmaf_results)
                print(f"   -> {q_label} {icq}: VMAF {avg_vmaf:.2f}")

                if avg_vmaf >= target_vmaf:
                    best_icq = icq
                    best_vmaf = avg_vmaf
                    completed_results.append({"icq": icq, "vmaf": round(avg_vmaf, 1)})
                    print(f"-> Tavoite-VMAF ({target_vmaf}) saavutettu {q_label} {icq}:lla.")
                    break

                is_full = len(vmaf_results) == len(ref_clips)
                if is_full and avg_vmaf >= min_vmaf and (fallback_icq is None or avg_vmaf > fallback_vmaf):
                    fallback_icq = icq
                    fallback_vmaf = avg_vmaf
                    print(f"   -> Varavalinta tallennettu: {q_label} {icq} (VMAF {avg_vmaf:.2f})")

                completed_results.append({"icq": icq, "vmaf": round(avg_vmaf, 1)})

            if best_vmaf == 0.0:
                if fallback_icq is not None:
                    best_icq = fallback_icq
                    best_vmaf = fallback_vmaf
                    print(f"-> Tavoite-VMAF ei saavutettu. Käytetään varavalintaa {q_label} {best_icq} (VMAF {best_vmaf:.2f}).")
                else:
                    best_icq = cands[-1]
                    print(f"-> Yksikään arvo ei saavuttanut tavoitetta. Käytetään parasta kandidaattia {q_label} {best_icq}.")

            return best_icq, best_vmaf if best_vmaf > 0 else None

        finally:
            shutil.rmtree(vmaf_tmp, ignore_errors=True)

    # --- YKSITTÄISEN TYÖN SUORITUS ---

    def process_job(self, job_data: dict):
        job_id = job_data["id"]
        self.current_job_id = job_id
        start_time = time.time()
        job_cfg = job_data.get("config", {})

        server_in_str = f"{job_data['input_dir']}/{job_data['rel_path']}"
        local_in_path = self.map_path(server_in_str)

        if not local_in_path.exists():
            raise FileNotFoundError(f"Lähdetiedostoa ei löydy polusta: {local_in_path} (Tarkista path_mapping!)")

        orig_size = local_in_path.stat().st_size
        dur, vid_idx, vid_codec, color_space, height, audios, subs = self.probe_video(local_in_path)
        
        allowed_langs = job_cfg.get("allowed_languages", [])
        drop_subs = job_cfg.get("drop_sub_codecs", [])
        audios, subs = self.filter_streams(audios, subs, allowed_langs, drop_subs)

        # Laatuasetuksen valinta: VMAF-haarukointi tai kiinteä arvo
        is_nvenc = "nvenc" in self.encoder.lower()
        is_amf = "amf" in self.encoder.lower()
        q_label = "CQ" if is_nvenc else ("QP" if is_amf else "CRF")

        chosen_q, vmaf_score = self.find_best_icq(
            input_path=local_in_path,
            vid_idx=vid_idx,
            height=height,
            duration=dur,
            job_cfg=job_cfg,
            job_data=job_data
        )

        vmaf_str = f" | VMAF: {vmaf_score:.2f}" if vmaf_score else ""
        print(f"\n{'='*70}")
        print(f"[TYÖ #{job_id}] {job_data['rel_path']}")
        print(f"-> Koodekki: {vid_codec} | Resoluutio: {height}p | Kesto: {dur:.1f}s")
        print(f"-> Valittu {q_label}: {chosen_q}{vmaf_str} (Enkooderi: {self.encoder})")
        print(f"{'='*70}\n")

        # Väliaikaistiedosto peli-PC:n paikalliselle levylle
        temp_output_path = self.temp_dir / f"encode_{job_id}.tmp.mkv"
        if temp_output_path.exists():
            temp_output_path.unlink(missing_ok=True)

        cmd = self.build_ffmpeg_cmd(
            input_path=local_in_path,
            output_path=temp_output_path,
            vid_idx=vid_idx,
            height=height,
            audios=audios,
            subs=subs,
            q_value=chosen_q,
            job_cfg=job_cfg
        )

        print(f"[FFMPEG] {' '.join(cmd)}\n")

        self.current_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        last_update = 0.0
        error_lines = []

        try:
            for line in self.current_proc.stdout:
                line_str = line.strip()
                error_lines.append(line_str)
                if len(error_lines) > 30:
                    error_lines.pop(0)

                # Parsitaan ffmpeg edistymistieto
                if "time=" in line_str and "fps=" in line_str:
                    now = time.time()
                    if now - last_update >= 1.0:
                        last_update = now

                        # Aika
                        t_str = "00:00:00"
                        cur_sec = 0.0
                        if "time=" in line_str:
                            import re
                            m = re.search(r"time=(\d{2}:\d{2}:\d{2}(?:\.\d+)?)", line_str)
                            if m:
                                t_str = m.group(1).split(".")[0]
                                parts = t_str.split(":")
                                cur_sec = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])

                        pct = round((cur_sec / dur) * 100, 1) if dur > 0 else None
                        if pct and pct > 100: pct = 100.0

                        fps_val = "-"
                        m_fps = re.search(r"fps=\s*([\d\.]+)", line_str)
                        if m_fps: fps_val = m_fps.group(1)

                        speed_val = "-"
                        m_speed = re.search(r"speed=\s*([\d\.x]+)", line_str)
                        if m_speed: speed_val = m_speed.group(1)

                        eta_sec = None
                        if dur > 0 and cur_sec > 0:
                            elapsed = now - start_time
                            rate = cur_sec / elapsed if elapsed > 0 else 0
                            if rate > 0:
                                eta_sec = int((dur - cur_sec) / rate)

                        p_data = {
                            "file": job_data["rel_path"],
                            "percent": pct,
                            "fps": fps_val,
                            "speed": speed_val,
                            "time": t_str,
                            "eta_sec": eta_sec,
                            "q_label": q_label,
                            "q_value": chosen_q,
                            "encoder": self.encoder,
                            "src_codec": vid_codec,
                            "height": height,
                        }

                        ctrl = self.report_progress(job_id, p_data)
                        if ctrl == "stop":
                            print("\n[STOP] Palvelin pyysi pysäyttämään työn!")
                            self.current_proc.terminate()
                            self.current_proc.wait(timeout=5)
                            raise WorkerStopException("Käyttäjä pysäytti työn palvelimelta")
                        elif ctrl == "pause":
                            print("\n[PAUSE] Työ asetettu tauolle palvelimelta...")
                            while True:
                                time.sleep(2)
                                s = self.heartbeat()
                                if s == "run":
                                    print("[RESUME] Jatketaan...")
                                    break
                                elif s == "stop":
                                    self.current_proc.terminate()
                                    raise WorkerStopException("Käyttäjä pysäytti työn pausesta")

            self.current_proc.wait()
            if self.current_proc.returncode != 0:
                err_msg = "\n".join(error_lines[-10:])
                raise RuntimeError(f"FFmpeg epäonnistui (koodi {self.current_proc.returncode}):\n{err_msg}")

        except WorkerStopException:
            if temp_output_path.exists():
                temp_output_path.unlink(missing_ok=True)
            self.release_job(job_id, "Pysäytetty käyttäjän toimesta")
            return

        # 1. Tarkistetaan lopputuloksen kesto
        new_dur = self.get_duration(temp_output_path)
        max_diff = job_cfg.get("max_duration_diff_sec", 10.0)
        if abs(dur - new_dur) > max_diff:
            if temp_output_path.exists(): temp_output_path.unlink()
            raise RuntimeError(f"Kestovirhe: Lähde {dur:.1f}s vs Valmis {new_dur:.1f}s (raja {max_diff}s)")

        # 2. Tarkistetaan pakkaussuhde
        new_size = temp_output_path.stat().st_size
        saved_pct = (1.0 - (new_size / orig_size)) * 100.0
        min_comp = job_cfg.get("min_compression_pct", 10.0)

        if saved_pct < min_comp:
            print(f"[OHITETTU] Säästö liian pieni ({saved_pct:.1f}% < {min_comp}%)")
            temp_output_path.unlink(missing_ok=True)
            if job_cfg.get("remux_on_skip", False):
                ext = job_cfg.get("remux_container", "mkv").lstrip(".")
                if job_data.get("in_place", False):
                    remux_final = local_in_path.with_suffix(f".{ext}")
                else:
                    remux_final = self.map_path(f"{job_data['output_dir']}/{job_data['rel_path']}").with_suffix(f".{ext}")
                print(f"  -> remux_on_skip=True: remuxataan {local_in_path.suffix} → .{ext} (stream-copy)...")
                remux_final.parent.mkdir(parents=True, exist_ok=True)
                cmd_remux = [
                    "ffmpeg", "-y",
                    "-fflags", "+genpts",
                    "-i", str(local_in_path),
                    "-c", "copy",
                    "-map", "0",
                    "-dn",
                    "-avoid_negative_ts", "make_zero",
                    "-map_chapters", "0",
                    str(remux_final)
                ]
                r_res = subprocess.run(cmd_remux, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                if r_res.returncode == 0:
                    r_size = remux_final.stat().st_size if remux_final.exists() else orig_size
                    print(f"  -> Remux valmis: {remux_final}")
                    if job_data.get("in_place", False) and local_in_path != remux_final:
                        local_in_path.unlink(missing_ok=True)
                    self.report_complete(job_id, {
                        "new_size_bytes": r_size,
                        "processing_time_sec": time.time() - start_time,
                        "encoder_used": f"remux_{ext}",
                        "vmaf_score": None,
                        "notes": f"Säästö liian pieni ({saved_pct:.1f}%) | Remuxattu .{ext} | Node: {self.node_name}",
                        "status": "completed",
                        "encode_bytes_written": r_size
                    })
                    return
                else:
                    print(f"  -> Remux epäonnistui: {r_res.stderr[-300:]}")

            self.report_complete(job_id, {
                "new_size_bytes": orig_size,
                "processing_time_sec": time.time() - start_time,
                "encoder_used": self.encoder,
                "vmaf_score": None,
                "notes": f"Säästö liian pieni ({saved_pct:.1f}%) | Node: {self.node_name}",
                "status": "skipped",
                "encode_bytes_written": 0
            })
            return

        # 3. Siirretään valmis tiedosto verkkolevylle
        if job_data.get("in_place", False):
            final_server_path = local_in_path.with_suffix(".mkv")
        else:
            server_out_str = f"{job_data['output_dir']}/{job_data['rel_path']}"
            final_server_path = self.map_path(server_out_str).with_suffix(".mkv")

        print(f"-> Siirretään valmis tiedosto kohteeseen: {final_server_path}")
        final_server_path.parent.mkdir(parents=True, exist_ok=True)

        # Turvallinen siirto väliaikaisen nimen kautta verkkolevylle
        staging_net_path = final_server_path.with_name(f"{final_server_path.stem}.part_{job_id}.mkv")
        shutil.copyfile(str(temp_output_path), str(staging_net_path))
        if final_server_path.exists():
            final_server_path.unlink()
        shutil.move(str(staging_net_path), str(final_server_path))
        temp_output_path.unlink(missing_ok=True)

        # Jos in_place ja vanha pääte oli eri (esim .mp4), poistetaan vanha
        if job_data.get("in_place", False) and local_in_path != final_server_path:
            local_in_path.unlink(missing_ok=True)

        proc_time = time.time() - start_time
        print(f"[VALMIS #{job_id}] Säästö: {saved_pct:.1f}% ({orig_size//1024//1024} MB -> {new_size//1024//1024} MB) ajassa {proc_time:.1f}s\n")

        vmaf_note = f" (VMAF: {vmaf_score:.1f})" if vmaf_score else ""
        self.report_complete(job_id, {
            "new_size_bytes": new_size,
            "processing_time_sec": proc_time,
            "encoder_used": self.encoder,
            "vmaf_score": vmaf_score,
            "notes": f"{q_label}: {chosen_q}{vmaf_note} | Säästö: {saved_pct:.1f}% | Node: {self.node_name}",
            "status": "completed",
            "encode_bytes_written": new_size
        })

    def run(self):
        """Pääsilmukka: kysyy töitä palvelimelta ja suorittaa niitä."""
        print(f"\n=======================================================")
        print(f"  Optivmaf Worker Node: '{self.node_name}' ({self.node_id})")
        print(f"  Master URL: {self.master_url}")
        print(f"  Enkooderi:  {self.encoder} (Preset: {self.speedpreset})")
        print(f"  Temp-levy:  {self.temp_dir}")
        print(f"=======================================================")
        print("Paina Ctrl+C milloin tahansa keskeyttääksesi.\n")

        while self.is_running:
            try:
                # 1. Heartbeat
                ctrl = self.heartbeat()
                if ctrl in ("stop", "pause"):
                    time.sleep(self.poll_interval)
                    continue

                # 2. Haetaan seuraava työ
                job = self.claim_job()
                if job:
                    try:
                        self.process_job(job)
                    except WorkerStopException as e:
                        print(f"\n[TYÖ #{job['id']} PYSÄYTETTY]: {e}")
                        self.release_job(job["id"], str(e))
                    except Exception as e:
                        print(f"\n[VIRHE TYÖSSÄ #{job['id']}]: {e}")
                        self.report_fail(job["id"], str(e))
                    finally:
                        self.current_job_id = None
                        self.current_proc = None
                else:
                    # Ei töitä jonossa, odotetaan
                    time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                print("\n[KESKEYTYS] Sammutetaan worker...")
                if self.current_job_id:
                    print(f"Vapautetaan käynnissä oleva työ #{self.current_job_id} takaisin jonoon...")
                    if self.current_proc:
                        try:
                            self.current_proc.terminate()
                            self.current_proc.wait(timeout=3)
                        except Exception:
                            pass
                    self.release_job(self.current_job_id, "Käyttäjä keskeytti workerin (Ctrl+C)")
                break
            except Exception as e:
                print(f"[YHTEYSHÄIRIÖ] Ei yhteyttä palvelimeen ({e}). Yritetään uudelleen {self.poll_interval}s kuluttua...")
                time.sleep(self.poll_interval)

        print("[WORKER] Sammutettu.")

def main():
    def _sig_handler(sig, frame):
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except Exception:
        pass

    worker = RemoteWorker()
    worker.run()

if __name__ == "__main__":
    main()
