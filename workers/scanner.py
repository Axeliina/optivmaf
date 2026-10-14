import os
from pathlib import Path
from sqlalchemy.orm import Session

from core.config import AppConfig
from core.database import VideoRecord, JobStatus, SessionLocal
from workers.probe import probe_video, ProbeError

def scan_library(config: AppConfig, session: Session) -> dict:
    """
    Käy läpi kaikki lähdehakemistot ja lisää uudet videot tietokantaan.
    Palauttaa tilastosanakirjan skannauksen tuloksista.
    """
    video_exts = {".mkv", ".mp4", ".avi", ".ts", ".wmv", ".mov", ".m4v", ".flv"}

    stats = {
        "new_added": 0,
        "already_exists": 0,
        "auto_skipped": 0,
        "failed_scans": 0
    }

    for folder in config.files.folders:
        input_dir = folder.input_dir
        if not input_dir.exists():
            print(f"[VIRHE] Lähdehakemistoa {input_dir} ei ole olemassa (Kansio: {folder.id}).")
            continue

        print(f"Aloitetaan hakemiston skannaus: {input_dir}")

        for root, _, files in os.walk(input_dir):
            for file in files:
                input_path = Path(root) / file

                if input_path.suffix.lower() not in video_exts:
                    continue

                rel_path = input_path.relative_to(input_dir)
                rel_path_str = str(rel_path)
                file_path_db = f"{folder.id}:{rel_path_str}"

                # 1. Tarkistetaan onko tiedosto jo tietokannassa
                existing_record = session.query(VideoRecord).filter_by(file_path=file_path_db).first()
                if existing_record:
                    stats["already_exists"] += 1
                    continue

                # 2. Uusi tiedosto -> Suoritetaan ffprobe
                try:
                    # probe_video palauttaa: dur, vid_idx, vid_codec, color_space, height, width, audios, subs
                    res = probe_video(input_path)
                    vid_idx = res[1]
                    vid_codec = res[2]
                    height = res[4]

                    if vid_idx is None:
                        print(f"[SKIP] {file_path_db}: Ei videoraitaa (mahdollinen pelkkä kuvatiedosto).")
                        status = JobStatus.SKIPPED
                        notes = "Skannaus: Ei videoraitaa"
                        stats["auto_skipped"] += 1
                    elif vid_codec in config.video.skip_codecs:
                        print(f"[SKIP] {file_path_db}: Ohitetaan (koodekki {vid_codec} on skippauslistalla).")
                        status = JobStatus.SKIPPED
                        notes = f"Skannaus: Koodekki ohitettu ({vid_codec})"
                        stats["auto_skipped"] += 1
                    else:
                        status = JobStatus.PENDING
                        notes = None
                        stats["new_added"] += 1
                        print(f"[LISÄTTY JONOON] {file_path_db} ({vid_codec}, {height}p)")

                    # Luodaan tietokantatietue
                    record = VideoRecord(
                        folder_id=folder.id,
                        file_path=file_path_db,
                        status=status,
                        orig_size_bytes=input_path.stat().st_size,
                        orig_codec=vid_codec if vid_codec else "unknown",
                        orig_height=height,
                        notes=notes
                    )
                    session.add(record)

                except (ProbeError, Exception) as e:
                    print(f"[VIRHE] Tiedoston {file_path_db} skannaus kaatui: {e}")
                    # Tallennetaan failattu status kantaan, jotta rikkinäiseen tiedostoon ei jäädä jumiin
                    # seuraavilla skannauskerroilla.
                    record = VideoRecord(
                        folder_id=folder.id,
                        file_path=file_path_db,
                        status=JobStatus.FAILED,
                        orig_size_bytes=input_path.stat().st_size if input_path.exists() else 0,
                        orig_codec="unknown",
                        orig_height=0,
                        notes=f"Skannaus kaatui: {str(e)}"
                    )
                    session.add(record)
                    stats["failed_scans"] += 1

    # Tallennetaan kaikki uudet löydökset kantaan kerralla transaktion päätteeksi
    session.commit()

    print("\n--- Skannaus valmis ---")
    print(f"Uudet jonossa: {stats['new_added']}")
    print(f"Ohitettu automaattisesti: {stats['auto_skipped']}")
    print(f"Jo tietokannassa: {stats['already_exists']}")
    print(f"Virheet: {stats['failed_scans']}")

    return stats

# Testausblokki, jos ajat tätä suoraan lokaalisti:
if __name__ == "__main__":
    from core.config import load_config

    try:
        cfg = load_config()
        db_session = SessionLocal()
        scan_library(cfg, db_session)
    finally:
        db_session.close()
