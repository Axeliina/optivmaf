import os
import subprocess
import json
import shutil
import tempfile
from pathlib import Path
from core.config import AppConfig
from core.progress import update_job_progress

def _write_progress(data: dict, job_id: int | str | None = None):
    """Kirjoittaa progress.json-tiedostoon virheitä vaientaen monityötukena."""
    jid = job_id or data.get("job_id", "default")
    update_job_progress(jid, data)

class EncoderError(Exception):
    def __init__(self, message, error_log=None):
        super().__init__(message)
        self.error_log = error_log

def build_ffmpeg_cmd(
    input_path: Path,
    output_path: Path,
    streams: dict,
    config: AppConfig,
    icq_crf: int,
    is_vmaf_test: bool = False,
    color_filter: str | None = None
) -> list[str]:

    cmd = ["ffmpeg", "-y"]

    enc_name = config.video.encoder.lower()
    is_vaapi = "vaapi" in enc_name
    is_nvenc = "nvenc" in enc_name
    hw_dev = getattr(config.video, 'hw_device', '/dev/dri/renderD128') or '/dev/dri/renderD128'

    if is_vaapi:
        cmd.extend([
            "-init_hw_device", f"vaapi=va:{hw_dev}",
            "-filter_hw_device", "va"
        ])

    # Metatietojen siivous lisätään output-puolelle (ei ennen -i:tä)
    cmd.extend(["-reinit_filter", "0", "-i", str(input_path), "-map", f"0:{streams['vid_idx']}"])

    video_filters = []
    if config.video.deinterlace:
        video_filters.append("yadif=1")

    v_stream = (streams.get("video") or [{}])[0]
    height = v_stream.get("height") or streams.get("height") or 0
    width = v_stream.get("width") or streams.get("width") or 0
    
    if height > 0:
        if config.video.max_resolution and height > config.video.max_resolution:
            target_h = config.video.max_resolution
            target_w = -2
            video_filters.append(f"scale={target_w}:{target_h}")
        elif width > 0:
            # Pakotetaan kiinteä resoluutio (estää kaatumiset jos reso muuttuu kesken videon)
            video_filters.append(f"scale={width}:{height}")

    if is_vaapi:
        vf_str = f"setparams={color_filter}," if color_filter else ""
        # force_10bit pakottaa p010le:n (10-bit), muutoin sallitaan myös nv12 (8-bit)
        fmt = "p010le" if config.video.force_10bit else "nv12|p010le"
        video_filters.append(f"{vf_str}format={fmt},hwupload")
    elif is_nvenc:
        if color_filter:
            video_filters.append(f"setparams={color_filter}")
        fmt = "p010le" if config.video.force_10bit else "yuv420p"
        video_filters.append(f"format={fmt}")
    else:
        # CPU-enkooderit (svtav1, x265, x264)
        if color_filter:
            video_filters.append(f"setparams={color_filter}")
        if config.video.force_10bit:
            video_filters.append("format=yuv420p10le")

    if video_filters:
        cmd.extend(["-filter:v", ",".join(video_filters)])

    cmd.extend(["-c:v", config.video.encoder])

    enc_speed = config.video.get_effective_speedpreset(enc_name)

    if "svtav1" in enc_name:
        cmd.extend(["-crf", str(icq_crf), "-preset", str(enc_speed)])
        # SVT-AV1 parametrit: film grain + tiles svtav1-params-kentässä
        svt_parts = []
        if config.video.film_grain > 0 and not is_vmaf_test:
            svt_parts.append(f"film-grain={config.video.film_grain}")
            if config.video.film_grain_denoise:
                svt_parts.append("film-grain-denoise=1")
        if config.video.tile_columns is not None:
            svt_parts.append(f"tile-columns={config.video.tile_columns}")
        if config.video.tile_rows is not None:
            svt_parts.append(f"tile-rows={config.video.tile_rows}")
        if svt_parts:
            cmd.extend(["-svtav1-params", ":".join(svt_parts)])
    elif is_vaapi:
        cmd.extend(["-global_quality", str(icq_crf), "-speed", str(enc_speed)])
    elif is_nvenc:
        # NVENC: -cq vakio laadulle (tai -qp), -preset p1-p7
        cmd.extend(["-rc", "vbr", "-cq", str(icq_crf), "-b:v", "0", "-preset", f"p{min(max(enc_speed, 1), 7)}"])
    else:
        # CPU x264 / x265 / fallback
        cmd.extend(["-crf", str(icq_crf), "-preset", str(enc_speed)])

    # Yhteiset videon parametrit
    if config.video.gop_size is not None:
        cmd.extend(["-g", str(config.video.gop_size)])
    if config.video.b_depth is not None:
        cmd.extend(["-bf", str(config.video.b_depth)])
        if config.video.b_strategy:
            cmd.extend(["-b_strategy", "1"])

    out_audio_idx = 0
    out_sub_idx = 0

    if is_vmaf_test:
        cmd.extend(["-an", "-sn"])
    else:
        for a in streams['audios']:
            codec_name = a.get("codec_name", "")
            channels = a.get("channels", 2)
            is_stereo = channels <= 2
            is_passthrough = codec_name in config.audio_subs.passthrough_codecs

            cmd.extend(["-map", f"0:{a['idx']}"])
            if is_passthrough:
                # Lossless/korkealaatuinen raita (TrueHD, DTS, EAC3) kopioidaan aina
                cmd.extend([f"-c:a:{out_audio_idx}", "copy"])
            elif config.audio_subs.smart_audio and is_stereo:
                cmd.extend([
                    f"-c:a:{out_audio_idx}", config.audio_subs.fallback_codec,
                    "-b:a", config.audio_subs.fallback_bitrate
                ])
            else:
                cmd.extend([f"-c:a:{out_audio_idx}", "copy"])
            out_audio_idx += 1

            # Stereodownmix: lisätään 2.0-raita monikanava-äänten rinnalle
            if config.audio_subs.add_stereo_downmix and not is_stereo:
                cmd.extend(["-map", f"0:{a['idx']}"])
                cmd.extend([
                    f"-c:a:{out_audio_idx}", config.audio_subs.stereo_downmix_codec,
                    "-b:a", config.audio_subs.stereo_downmix_bitrate,
                    "-ac", "2"
                ])
                out_audio_idx += 1

        for s in streams['subs']:
            if s.get("codec_name") in config.audio_subs.drop_sub_codecs:
                continue
            cmd.extend(["-map", f"0:{s['idx']}"])
            # ffmpeg muuntaa subrip → ass automaattisesti MKV-kontainerissa.
            # Pakotetaan subrip pysymään subrip:nä; muut kopioidaan sellaisenaan.
            if s.get("codec_name") == "subrip":
                cmd.extend([f"-c:s:{out_sub_idx}", "subrip"])
            else:
                cmd.extend([f"-c:s:{out_sub_idx}", "copy"])
            out_sub_idx += 1

    cmd.append("-dn")

    if config.video.custom_flags:
        cmd.extend(config.video.custom_flags)

    if not is_vmaf_test:
        # Metatietojen siivous: output-optio, tulee -i:n jälkeen
        if config.container.clean_metadata:
            cmd.extend(["-map_metadata", "-1"])

        # Oletusraitojen normalisointi: 1. audio = default, tekstitykset = ei-default
        if config.container.normalize_default_tracks:
            for i in range(out_audio_idx):
                cmd.extend([f"-disposition:a:{i}", "default" if i == 0 else "0"])
            for i in range(out_sub_idx):
                cmd.extend([f"-disposition:s:{i}", "0"])

        # Kappaleiden säilytys
        if config.container.keep_chapters:
            cmd.extend(["-map_chapters", "0"])

    cmd.append(str(output_path))

    return cmd



def find_best_icq(
    input_path: Path,
    streams: dict,
    duration: float,
    config: AppConfig,
    progress_base: dict | None = None
) -> tuple[int | None, float, str | None, dict]:

    if getattr(config.video, 'skip_vmaf', False):
        fixed_val = getattr(config.video, 'fixed_icq', 24)
        print(f"-> VMAF-tarkistus ohitettu asetuksista, käytetään kiinteää ICQ:ta {fixed_val}.")
        return fixed_val, 0.0, None, {"bytes_written": 0, "storage": "none"}

    enc_name = config.video.encoder.lower()
    if "nvenc" in enc_name:
        q_label = "CQ"
    elif "vaapi" in enc_name:
        q_label = "ICQ"
    else:
        q_label = "CRF"

    v_stream = (streams.get("video") or [{}])[0]
    height = v_stream.get("height") or streams.get("height") or 1080

    # Jos video skaalataan max_resolution-asetuksella, käytetään tulosresoluutiota kandidaattien valintaan
    effective_height = min(height, config.video.max_resolution) if (config.video.max_resolution and height > 0) else height

    if effective_height >= 1440:
        icq_candidates = config.video.vmaf_icq_candidates_4k
    elif effective_height >= 1000:
        icq_candidates = config.video.vmaf_icq_candidates_1080p
    else:
        icq_candidates = config.video.vmaf_icq_candidates_sd


    clip_len = config.video.vmaf_clip_length_sec
    if duration < clip_len * 3:
        fallback = icq_candidates[len(icq_candidates) // 2]
        return fallback, 0.0, None, {"bytes_written": 0, "storage": "none"}

    # Kolme leikkauspistettä (20%, 50%, 80%) — sama kuin alkuperäisessä skriptissä
    percentages = [0.20, 0.50, 0.80]
    best_icq = icq_candidates[0]
    best_vmaf = 0.0

    print(f"-> Leikataan 3 kpl {clip_len}s näytteitä VMAF-testiä varten...")

    # Mitkä tiedostopäätteet käyttävät FFV1-lossless-referenssiä VMAF-testauksessa.
    # Normalisoidaan pienaakkosiksi vertailua varten (config voi sisältää isoja tai pieniä kirjaimia).
    lossless_exts = {ext.lower() if ext.startswith('.') else f'.{ext.lower()}'
                     for ext in config.video.vmaf_lossless_ref_exts}
    use_lossless_ref = input_path.suffix.lower() in lossless_exts
    mode_label = "FFV1/lossless" if use_lossless_ref else "stream-copy"

    # VMAF-temp-hakemisto: RAM (/dev/shm) tai levy (järjestelmän temp)
    # src_size_per_clip lasketaan ensin, jotta voidaan tarkistaa onko RAM-levyllä tarpeeksi tilaa.
    src_size_per_clip = (input_path.stat().st_size / duration) * clip_len if duration > 0 else 0

    # FFV1-lossless on tyypillisesti 15–25x suurempi kuin pakattu lähde.
    # Varataan tilaa: 3 ref-klipille (FFV1 tai copy) + 3 test-klipille kerrallaan (AV1).
    ffv1_factor = 20 if use_lossless_ref else 2
    n_clips = len(percentages)  # = 3
    est_needed_bytes = src_size_per_clip * n_clips * ffv1_factor + src_size_per_clip * n_clips

    use_ramdisk = config.video.vmaf_use_ramdisk
    ramdisk_path = Path("/dev/shm")
    if use_ramdisk and ramdisk_path.exists():
        shm_free = shutil.disk_usage(ramdisk_path).free
        shm_free_mb = shm_free // (1024 ** 2)
        est_needed_mb = int(est_needed_bytes // (1024 ** 2)) + 1
        if shm_free < est_needed_bytes:
            print(f"-> [VAROITUS] /dev/shm liian täynnä ({shm_free_mb} MB vapaana, tarvitaan arv. {est_needed_mb} MB) — käytetään levyä")
            use_ramdisk = False
        else:
            print(f"-> VMAF-temp: RAM-muisti ({ramdisk_path}) | vapaana {shm_free_mb} MB, tarvitaan arv. {est_needed_mb} MB")

    if use_ramdisk:
        tmpdir_path = Path(tempfile.mkdtemp(dir=ramdisk_path, prefix="optivmaf_vmaf_"))
    else:
        if config.video.vmaf_use_ramdisk and not use_ramdisk:
            pass  # varoitus jo tulostettu yllä
        elif not config.video.vmaf_use_ramdisk:
            print(f"-> VMAF-temp: levy (järjestelmän temp)")
        tmpdir_path = Path(tempfile.mkdtemp(prefix="optivmaf_vmaf_"))

    total_vmaf_bytes = 0
    try:
        tmp = tmpdir_path
        ref_clips = []

        # src_size_per_clip on jo laskettu yllä tilantarkistusta varten

        # 1. Leikataan ref-klipit
        #    Ongelmalliset kontainerit (MP4/WMV) dekoodataan häviöttömästi FFV1:lle
        #    jotta aikataulut alkavat taatusti nollasta progressiivisina ruutuina.
        #    Muille käytetään nopeaa stream-copy:a (-avoid_negative_ts make_zero).
        for i, pct in enumerate(percentages):
            start_t = max(0, duration * pct)
            ref_clip = tmp / f"ref_{i}.mkv"
            if use_lossless_ref:
                # Dekoodaa raakapikseleiksi → pakkaa FFV1:lle
                # -vf setpts=PTS-STARTPTS nollaa timestampit heti dekoodin jälkeen
                cmd_ref = [
                    "ffmpeg", "-y",
                    "-ss", str(start_t),
                    "-i", str(input_path),
                    "-t", str(clip_len),
                    "-map", f"0:{streams['vid_idx']}",
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
                    "-map", f"0:{streams['vid_idx']}",
                    "-c:v", "copy",
                    "-an", "-sn",
                    "-avoid_negative_ts", "make_zero",
                    str(ref_clip)
                ]
            result = subprocess.run(cmd_ref, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if ref_clip.exists() and ref_clip.stat().st_size > 0:
                ref_clips.append(ref_clip)
                c_sz = ref_clip.stat().st_size
                total_vmaf_bytes += c_sz
                print(f"   ref_{i}.mkv: {c_sz // 1024} KB @ {start_t:.0f}s [{mode_label}]")
            else:
                print(f"   [VAROITUS] ref_{i}.mkv luonti epäonnistui: {result.stderr[-300:]}")

        if not ref_clips:
            print("   [VIRHE] Referenssiklippien luonti epäonnistui.")
            return best_icq, 0.0, None, {"bytes_written": total_vmaf_bytes, "storage": "ram" if use_ramdisk else "disk"}

        # 2. Väriavaruuden arvaukset — Unknown color_space kokeillaan korjata
        color_space = streams.get("color_space", "unknown").lower()
        color_guesses: list[str | None] = [None]
        if color_space == "unknown" and config.video.test_color_guesses:
            color_guesses.extend([
                "colorspace=bt709:color_primaries=bt709:color_trc=bt709",
                "colorspace=smpte170m:color_primaries=smpte170m:color_trc=smpte170m"
            ])

        enc_name = config.video.encoder.lower()
        is_vaapi = "vaapi" in enc_name
        is_nvenc = "nvenc" in enc_name
        hw_dev = getattr(config.video, 'hw_device', '/dev/dri/renderD128') or '/dev/dri/renderD128'

        if is_vaapi:
            print(f"-> Testataan {q_label}-arvoja ({config.video.encoder}) VAAPI-laitteella ({hw_dev})...")
        elif is_nvenc:
            print(f"-> Testataan {q_label}-arvoja ({config.video.encoder}) NVIDIA NVENC -kiihdytyksellä...")
        else:
            print(f"-> Testataan {q_label}-arvoja ({config.video.encoder}) prosessorilla...")
        completed_icq_results = []

        best_color_filter: str | None = None
        fallback_icq: int | None = None
        fallback_vmaf: float = 0.0
        fallback_color_filter: str | None = None

        for c_filter in color_guesses:
            if c_filter:
                print(f"   -> Kokeillaan väriavaruuden pakotusta: {c_filter.split(':')[0]}")

            found_target = False

            for icq in icq_candidates:
                # Tarkistetaan kontrollitila (stop)
                ctrl_path = Path(os.environ.get("OPTIVMAF_CONTROL_FILE", "data/control.json"))
                try:
                    with open(ctrl_path, "r") as cf:
                        if json.load(cf).get("state") == "stop":
                            print("[CTRL] Stop-signaali vastaanotettu VMAF-testauksen aikana.")
                            raise EncoderError("Käyttäjä pysäytti enkoodauksen (Stop VMAF-testissä).")
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

                vmaf_results = []
                test_sizes = []
                src_sizes = []  # Alkuperäisen bitstreamiosuuden arvioitu koko (ei FFV1-ref)

                # Päivitetään UI:n progress
                base = progress_base or {}
                _write_progress({
                    **base,
                    "vmaf_testing": True,
                    "vmaf_current_icq": icq,
                    "vmaf_results": completed_icq_results,
                    "vmaf_candidates": icq_candidates,
                    "vmaf_bytes_written": total_vmaf_bytes,
                    "vmaf_storage": "ram" if use_ramdisk else "disk",
                })

                for i, ref_clip in enumerate(ref_clips):
                    test_clip = tmp / f"test_{icq}_{i}.mkv"
                    vmaf_log  = tmp / f"vmaf_{icq}_{i}.json"

                    # Rakennetaan enkoodauskomento näytteelle
                    cmd_enc = ["ffmpeg", "-y"]
                    test_vfilters = []

                    if is_vaapi:
                        cmd_enc.extend([
                            "-init_hw_device", f"vaapi=va:{hw_dev}",
                            "-filter_hw_device", "va"
                        ])
                        fmt = "p010le" if config.video.force_10bit else "nv12|p010le"
                        vf_str = f"setparams={c_filter}," if c_filter else ""
                        test_vfilters.append(f"{vf_str}format={fmt},hwupload")
                    elif is_nvenc:
                        if c_filter:
                            test_vfilters.append(f"setparams={c_filter}")
                        fmt = "p010le" if config.video.force_10bit else "yuv420p"
                        test_vfilters.append(f"format={fmt}")
                    else:
                        if c_filter:
                            test_vfilters.append(f"setparams={c_filter}")
                        if config.video.force_10bit:
                            test_vfilters.append("format=yuv420p10le")

                    cmd_enc.extend(["-i", str(ref_clip)])
                    if test_vfilters:
                        cmd_enc.extend(["-vf", ",".join(test_vfilters)])

                    cmd_enc.extend(["-c:v", config.video.encoder])

                    test_speed = config.video.get_effective_speedpreset(enc_name)
                    if "svtav1" in enc_name:
                        cmd_enc.extend(["-crf", str(icq), "-preset", str(test_speed)])
                    elif is_vaapi:
                        cmd_enc.extend(["-global_quality", str(icq), "-speed", str(test_speed)])
                    elif is_nvenc:
                        cmd_enc.extend(["-rc", "vbr", "-cq", str(icq), "-b:v", "0", "-preset", f"p{min(max(test_speed, 1), 7)}"])
                    else:
                        cmd_enc.extend(["-crf", str(icq), "-preset", str(test_speed)])

                    cmd_enc.extend(["-an", "-sn", str(test_clip)])
                    enc = subprocess.run(cmd_enc, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

                    if enc.returncode != 0 or not test_clip.exists():
                        print(f"   [ENC VIRHE] {q_label} {icq} klippi {i}: {enc.stderr[-400:]}")
                        continue

                    enc_sz = test_clip.stat().st_size
                    total_vmaf_bytes += enc_sz
                    enc_kb = enc_sz // 1024
                    ref_kb = ref_clip.stat().st_size // 1024
                    print(f"   {q_label} {icq} klippi {i}: enkoodattu {enc_kb} KB (ref {ref_kb} KB)")

                    # VMAF-laskenta:
                    # - N/FRAME_RATE/TB generoi aikaleimat kehysindeksistä → tarkka ruututason synkronointi
                    #   riippumatta lähteen alkuperäisistä aikaleimoista (parempi kuin PTS-STARTPTS)
                    # - format-suodatin normalisoi bittisyvyyden ennen libvmaf:ia → eliminoi pikselitason
                    #   vääristymät 8-bit vs 10-bit mismatchin takia
                    vmaf_fmt = "yuv420p10le" if config.video.force_10bit else "yuv420p"
                    cmd_vmaf = [
                        "ffmpeg-vmaf", "-y",
                        "-i", str(test_clip),
                        "-i", str(ref_clip),
                        "-filter_complex",
                        f"[0:v]format={vmaf_fmt},setpts=N/FRAME_RATE/TB[dist];"
                        f"[1:v]format={vmaf_fmt},setpts=N/FRAME_RATE/TB[ref];"
                        f"[dist][ref]libvmaf=log_fmt=json:log_path={vmaf_log}",
                        "-f", "null", "-"
                    ]
                    try:
                        vmaf_proc = subprocess.run(
                            cmd_vmaf,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            text=True
                        )
                        if vmaf_proc.returncode != 0:
                            print(f"   [VMAF VIRHE] {q_label} {icq} klippi {i}: {vmaf_proc.stderr[-300:]}")
                            continue
                    except FileNotFoundError:
                        print("   [VIRHE] ffmpeg-vmaf -binääriä ei löydy! Tarkista polku.")
                        return best_icq, 0.0, None, {"bytes_written": total_vmaf_bytes, "storage": "ram" if use_ramdisk else "disk"}

                    if not vmaf_log.exists():
                        print(f"   [VAROITUS] vmaf_log ei syntynyt: {vmaf_log}")
                        continue

                    try:
                        with open(vmaf_log, "r") as f:
                            vmaf_val = json.load(f)["pooled_metrics"]["vmaf"]["mean"]
                        vmaf_results.append(vmaf_val)
                        test_sizes.append(test_clip.stat().st_size)
                        src_sizes.append(src_size_per_clip)
                        print(f"   {q_label} {icq} klippi {i}: VMAF={vmaf_val:.2f}")

                        # Pikahylkäys 1. klipin jälkeen:
                        # Jos tulos jää liikaa tavoitteesta (oletuksena > 2.0 pistettä), ei tuhlata aikaa
                        # muihin klippeihin, vaan hypätään suoraan seuraavaan laatuun.
                        # Viimeistä kandidaattia ei hylätä, jotta saadaan koko mittaus talteen (fallback/skip).
                        if i == 0 and len(ref_clips) > 1:
                            is_last_cand = (icq == icq_candidates[-1])
                            reject_margin = getattr(config.video, 'vmaf_early_reject_margin', 2.0)
                            if not is_last_cand and reject_margin > 0:
                                threshold = config.video.target_vmaf - reject_margin
                                if vmaf_val < threshold:
                                    print(f"   -> {q_label} {icq} klippi 0 (VMAF {vmaf_val:.2f}) < kynnys {threshold:.1f} (tavoite {config.video.target_vmaf} - {reject_margin:.1f}). Ohitetaan loput {len(ref_clips) - 1} klippiä.")
                                    break
                    except Exception as e:
                        print(f"   [VIRHE] vmaf_log lukeminen: {e}")

                if not vmaf_results:
                    print(f"   -> {q_label} {icq}: VMAF-mittaus epäonnistui kokonaan.")
                    completed_icq_results.append({"icq": icq, "vmaf": None})
                    continue

                avg_vmaf = sum(vmaf_results) / len(vmaf_results)
                size_pct = (sum(test_sizes) / sum(src_sizes)) * 100 if src_sizes else 100.0

                print(f"   -> {q_label} {icq}: VMAF {avg_vmaf:.2f} | Koko: {size_pct:.1f}%")

                # Koko kasvaa liikaa — lopetetaan tämän väriavaruuden testaus
                if size_pct >= 90.0:
                    print(f"   ! {q_label} {icq}: tiedostokoko kasvaa liikaa (>= 90%). Lopetetaan tällä värillä.")
                    completed_icq_results.append({"icq": icq, "vmaf": round(avg_vmaf, 1)})
                    break

                # Tavoite saavutettu
                if avg_vmaf >= config.video.target_vmaf:
                    best_icq = icq
                    best_vmaf = avg_vmaf
                    best_color_filter = c_filter
                    completed_icq_results.append({"icq": icq, "vmaf": round(avg_vmaf, 1)})
                    print(f"-> Tavoite-VMAF ({config.video.target_vmaf}) saavutettu {q_label} {icq}:lla.")
                    found_target = True
                    break

                # MIN_VMAF-fallback: talletetaan paras "riittävä" tulos
                # Vain täysin testatuista kandidaateista, jotta pikahylätty 1-klipin tulos ei lukitse huonompaa fallbackia
                is_full_test = len(vmaf_results) == len(ref_clips)
                if is_full_test and avg_vmaf >= config.video.min_vmaf and (fallback_icq is None or avg_vmaf > fallback_vmaf):
                    fallback_icq = icq
                    fallback_vmaf = avg_vmaf
                    fallback_color_filter = c_filter
                    print(f"   -> Varavalinta tallennettu: {q_label} {icq} (VMAF {avg_vmaf:.2f})")

                completed_icq_results.append({"icq": icq, "vmaf": round(avg_vmaf, 1)})

            if found_target:
                break  # Tavoite löytyi — ei kokeilla enää väriavaruuksia

        # Valitaan lopullinen ICQ
        if best_vmaf == 0.0:
            if fallback_icq is not None:
                best_icq = fallback_icq
                best_vmaf = fallback_vmaf
                best_color_filter = fallback_color_filter
                print(f"-> Tavoite-VMAF ei saavutettu. Käytetään varavalintaa {q_label} {best_icq} (VMAF {best_vmaf:.2f}).")
            else:
                # Ei yksikään ICQ saavuttanut edes min-VMAF — video hylätään
                print(f"-> SKIP: yksikään {q_label} ei saavuttanut min-VMAF {config.video.min_vmaf}. Video ohitetaan.")
                return None, 0.0, None, {"bytes_written": total_vmaf_bytes, "storage": "ram" if use_ramdisk else "disk"}

    finally:
        shutil.rmtree(tmpdir_path, ignore_errors=True)

    return best_icq, best_vmaf, best_color_filter, {"bytes_written": total_vmaf_bytes, "storage": "ram" if use_ramdisk else "disk"}
