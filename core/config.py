import json
from pathlib import Path
from pydantic import BaseModel, Field

class VideoSettings(BaseModel):
    encoder: str = Field(default="av1_vaapi")
    hw_device: str = Field(default="/dev/dri/renderD128", description="Laitteistokiihdytyslaitteen polku VAAPI:lle (esim. /dev/dri/renderD128)")
    max_concurrent_jobs: int = Field(default=1, ge=1, le=8, description="Samanaikaisesti enkoodattavien töiden määrä (esim. 2 GPU:lle tai useampi moniydin-CPU:lle)")
    encoder_speedpreset: int | None = Field(default=None, description="Yleinen nopeus/preset override. null = käytä enkooderikohtaisia asetuksia.")
    preset_vaapi: int = Field(default=1, ge=0, le=7, description="VAAPI -speed 0–7 (0=hidas/paras laatu, 7=nopein)")
    preset_svtav1: int = Field(default=6, ge=0, le=13, description="SVT-AV1 -preset 0–13 (0=hitain, 13=nopein, 6–8 suositus)")
    preset_nvenc: int = Field(default=5, ge=1, le=7, description="NVENC -preset 1–7 (p1-p7, p7=paras laatu)")
    preset_cpu_fallback: int = Field(default=5, ge=0, le=9, description="x264/x265 -preset 0–9")

    def get_effective_speedpreset(self, enc_name: str | None = None) -> int:
        """Palauttaa kyseiselle enkooderille oikean nopeus/preset-arvon."""
        if self.encoder_speedpreset is not None:
            return self.encoder_speedpreset
        target_enc = (enc_name or self.encoder).lower()
        if "vaapi" in target_enc:
            return self.preset_vaapi
        elif "svtav1" in target_enc:
            return self.preset_svtav1
        elif "nvenc" in target_enc:
            return self.preset_nvenc
        else:
            return self.preset_cpu_fallback
    target_vmaf: float = Field(default=93.0)
    min_vmaf: float = Field(default=91.0)

    # VMAF-haarukointi ja testaus
    vmaf_icq_candidates_4k: list[int] = Field(default=[36, 33, 30, 27, 24])
    vmaf_icq_candidates_1080p: list[int] = Field(default=[30, 27, 24, 21, 18])
    vmaf_icq_candidates_sd: list[int] = Field(default=[28, 25, 22, 19, 16])
    vmaf_clip_length_sec: int = Field(default=5)
    vmaf_early_reject_margin: float = Field(
        default=2.0,
        description="VMAF-haravoinnin pikahylkäysmarginaali: jos 1. klipin VMAF jää yli tämän verran tavoitteesta, siirrytään suoraan seuraavaan ICQ:hon (0 = pois käytöstä)."
    )
    skip_vmaf: bool = Field(default=False, description="Ohita koko VMAF-tarkistus ja käytä kiinteää ICQ-arvoa.")
    fixed_icq: int = Field(default=24, description="Kiinteä ICQ-arvo, jos VMAF-tarkistus ohitetaan.")

    # Legacy-videoiden (avi, wmv) korjaus
    legacy_fixed_icq: int = Field(default=26)

    skip_codecs: list[str] = Field(default=["av1", "hevc"])
    max_resolution: int | None = Field(default=None)
    min_compression_pct: float = Field(default=10.0)

    # Film grain
    film_grain: int = Field(default=0, ge=0, le=50)
    film_grain_denoise: bool = Field(default=False)

    deinterlace: bool = Field(default=False)

    # Värit ja HDR
    force_bt709_fallback: bool = Field(default=True)
    test_color_guesses: bool = Field(
        default=False,
        description="Jos VMAF on huono, kokeile väriavaruuden muunnosta (bt709/smpte170m) ja testaa uudestaan."
    )
    preserve_hdr: bool = Field(default=True)

    # Koodauslaatuparametrit
    force_10bit: bool = Field(default=False, description="Pakottaa p010le (10-bit) VAAPI:lla — eliminoi bandingin")
    gop_size: int | None = Field(default=None, description="GOP-pituus kehyksinä (-g). null = enkooderin oletus")
    b_depth: int | None = Field(default=None, description="B-kehysten määrä (-bf). null = enkooderin oletus")
    b_strategy: bool = Field(default=True, description="Salli enkooderin dynaamisesti muuttaa B-kehysten käyttöä (-b_strategy 1)")
    tile_columns: int | None = Field(default=None, description="AV1 tile columns (VAAPI: -tile_columns, SVT-AV1: tile-columns)")
    tile_rows: int | None = Field(default=None, description="AV1 tile rows (VAAPI: -tile_rows, SVT-AV1: tile-rows)")

    custom_flags: list[str] = Field(default_factory=list)

    # VMAF-testileikkeiden tallennuspaikka
    vmaf_use_ramdisk: bool = Field(
        default=False,
        description="Tallentaa VMAF-testileikkeet RAM-muistiin (/dev/shm) kiintolevyn sijaan. Nopeuttaa testausta mutta vaatii riittävästi vapaata RAM-muistia."
    )
    vmaf_lossless_ref_exts: list[str] = Field(
        default=[".mp4", ".wmv", ".avi", ".m4v", ".mov"],
        description="Tiedostopäätteet, joille käytetään FFV1-lossless-referenssiklippejä VMAF-testauksessa. Tyäjä lista = stream-copy kaikille. Esim. [\".wmv\", \".avi\"] rajoittaa vain niihin."
    )

    # Remux ohitetuille videoille
    remux_on_skip: bool = Field(
        default=False,
        description="Jos video ohitetaan (VMAF liian huono tai säästö liian pieni), remuxataan se silti uuteen konttiin stream-copy:lla ilman laadun menetystä."
    )
    remux_container: str = Field(
        default="mkv",
        description="Remux-kohdeformaatti ohitetuille videoille: 'mkv' tai 'mp4'."
    )

class AudioSubSettings(BaseModel):
    allowed_languages: list[str] = Field(default=["fin", "eng", "und", ""])
    smart_audio: bool = Field(default=True)
    fallback_codec: str = Field(default="aac", description="Mihin stereot pakataan")
    fallback_bitrate: str = Field(default="384k")

    # Passthrough: nämä koodekkit kopioidaan aina sellaisenaan
    passthrough_codecs: list[str] = Field(
        default=["truehd", "dts", "eac3"],
        description="Nämä koodekkit kopioidaan aina (-c:a copy), smart_audio-asetuksesta riippumatta"
    )
    # Stereodownmix: lisää erillinen stereoraita monikanava-äänelle
    add_stereo_downmix: bool = Field(default=False, description="Lisää 2.0-stereoraita monikanava-äänten rinnalle")
    stereo_downmix_codec: str = Field(default="aac")
    stereo_downmix_bitrate: str = Field(default="192k")

    drop_sub_codecs: list[str] = Field(default=[], description="Pudotettavat tekstitysformaatit (esim. hdmv_pgs_subtitle, dvd_subtitle, ass). Tyhjä = säilytä kaikki.")

class FolderMapping(BaseModel):
    id: str
    input_dir: Path
    output_dir: Path

class FileSettings(BaseModel):
    folders: list[FolderMapping] = Field(default_factory=list)
    # Legacy fields
    input_dir: Path | None = None
    output_dir: Path | None = None
    temp_dir: Path
    in_place: bool = Field(default=False)
    keep_extras: bool = Field(default=True)
    auto_scan_interval_minutes: int = Field(default=0, description="Automaattinen skannausväli minuutteina (0 = ei automaattista skannausta)")

class SafetySettings(BaseModel):
    min_free_space_gb: float = Field(default=50.0)
    max_duration_diff_sec: float = Field(default=2.0)
    max_consecutive_errors: int = Field(default=3, description="Jono pysähtyy tämän jälkeen")
    keep_failed_videos: bool = Field(default=False, description="Säilytä virheelliset tai hylätyt (esim. liian iso tai väärä kesto) videot .FAILED.mkv-päätteellä")

class ContainerSettings(BaseModel):
    clean_metadata: bool = Field(default=False, description="Siivoa enkoodausmetatiedot lähteestä (-map_metadata -1)")
    keep_chapters: bool = Field(default=True, description="Säilytä kappaleet (-map_chapters 0)")
    normalize_default_tracks: bool = Field(default=False, description="Normalisoi oletusraidat: 1. audio=default, tekstitykset=ei-default")

class AppConfig(BaseModel):
    video: VideoSettings
    audio_subs: AudioSubSettings
    files: FileSettings
    safety: SafetySettings
    container: ContainerSettings = Field(default_factory=ContainerSettings)

def load_config(config_path: str | Path = "data/config.json") -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Asetustiedostoa {config_path} ei löydy.")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    # Automaattinen migraatio vanhasta yleisestä encoder_speedpresetistä enkooderikohtaisiin
    if "video" in data:
        v = data["video"]
        old_sp = v.get("encoder_speedpreset")
        enc = v.get("encoder", "").lower()
        if old_sp is not None:
            if "vaapi" in enc and "preset_vaapi" not in v:
                v["preset_vaapi"] = min(max(old_sp, 0), 7)
            elif "svtav1" in enc and "preset_svtav1" not in v:
                v["preset_svtav1"] = min(max(old_sp, 0), 13)
            elif "nvenc" in enc and "preset_nvenc" not in v:
                v["preset_nvenc"] = min(max(old_sp, 1), 7)
            # Tyhjennetään yleinen override, jotta enkooderin vaihto käyttää kyseisen enkooderin omaa preset-arvoa
            v["encoder_speedpreset"] = None

    # Automaattinen migraatio vanhoista input_dir/output_dir -asetuksista uuteen folders-listaan
    if "files" in data:
        files = data["files"]
        if "folders" not in files or not files["folders"]:
            if "input_dir" in files and "output_dir" in files:
                files["folders"] = [{
                    "id": "oletus",
                    "input_dir": files["input_dir"],
                    "output_dir": files["output_dir"]
                }]
                
    return AppConfig(**data)
