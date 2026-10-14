import subprocess
import json
from pathlib import Path

class ProbeError(Exception):
    pass

def get_duration(file_path: Path) -> float:
    """
    Hakee tiedoston keston sekunteina nopeasti.
    Käytetään pakatun tiedoston eheyden varmistamiseen.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise ProbeError(f"ffprobe keston haku epäonnistui tiedostolle {file_path.name}:\n{res.stderr}")

    try:
        return float(res.stdout.strip())
    except ValueError:
        raise ProbeError(f"ffprobe palautti virheellisen keston tiedostolle {file_path.name}: {res.stdout}")

def probe_video(file_path: Path):
    """
    Lukee mediatiedoston stream-tiedot.
    Palauttaa: (duration, vid_idx, vid_codec, color_space, height, width, audio_tracks, sub_tracks)
    """
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(file_path)
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise ProbeError(f"ffprobe skannaus epäonnistui tiedostolle {file_path.name}")

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise ProbeError(f"ffprobe ei palauttanut kelvollista JSON-dataa tiedostolle {file_path.name}")

    raw_dur = data.get("format", {}).get("duration")
    try:
        duration = float(raw_dur) if raw_dur is not None else 0.0
    except (ValueError, TypeError):
        duration = 0.0

    video_idx = None
    video_codec = None
    color_space = "unknown"
    height = 0
    width = 0
    audio_tracks = []
    sub_tracks = []

    for s in data.get("streams", []):
        ctype = s.get("codec_type")
        cname = s.get("codec_name", "")
        idx = s.get("index")

        # Ohitetaan kansikuvat ja vastaavat liitteet
        disposition = s.get("disposition", {})
        if disposition.get("attached_pic") == 1 or cname in ["mjpeg", "png", "gif", "bmp"]:
            continue

        lang = s.get("tags", {}).get("language", "und").lower()

        if ctype == "video" and video_idx is None:
            video_idx = idx
            video_codec = cname.lower()
            color_space = s.get("color_space", "unknown").lower()
            try:
                height = int(s.get("height") or 0)
            except (ValueError, TypeError):
                height = 0
            try:
                width = int(s.get("width") or 0)
            except (ValueError, TypeError):
                width = 0

        elif ctype == "audio":
            audio_tracks.append({
                "idx": idx,
                "lang": lang,
                "channels": int(s.get("channels", 2)),
                "codec_name": cname.lower()
            })

        elif ctype == "subtitle":
            sub_tracks.append({
                "idx": idx,
                "lang": lang,
                "codec_name": cname.lower()
            })

    return duration, video_idx, video_codec, color_space, height, width, audio_tracks, sub_tracks

def filter_streams(audio_tracks: list[dict], sub_tracks: list[dict], allowed_languages: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Suodattaa ääni- ja tekstitysraidat sallittujen kielten perusteella.
    Palauttaa suodatetut listat (sisältävät yhä alkuperäiset sanakirjat, jotta idx/channels/codec säilyy).
    """
    keep_audio = []
    keep_subs = []

    # 1. Ääniraidat
    if len(audio_tracks) == 1:
        # Jos vain yksi raita, pidetään se kielestä riippumatta
        keep_audio.append(audio_tracks[0])
    else:
        for a in audio_tracks:
            if a["lang"] in allowed_languages:
                keep_audio.append(a)

        # Failsafe: Jos mikään ei täsmännyt, mutta raitoja on olemassa, valitaan ensimmäinen
        if not keep_audio and audio_tracks:
            keep_audio.append(audio_tracks[0])

    # 2. Tekstitykset
    for s in sub_tracks:
        if s["lang"] in allowed_languages:
            keep_subs.append(s)

    return keep_audio, keep_subs
