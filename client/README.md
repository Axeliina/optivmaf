# Optivmaf Hajautettu Worker (Peli-PC / Työasema)

Tämän ohjeen avulla voit lainata peli-PC:si tai minkä tahansa muun verkkokoneen suoritin- tai näytönohjaintehoa Optivmaf-videoiden enkoodaamiseen.

Agentti toimii täysin itsenäisesti eikä vaadi ulkoisia `pip`-kirjastoja. Se osaa pakata videot joko suorittimella (`libsvtav1`) tai näytönohjaimella (`av1_nvenc` / `hevc_nvenc` Nvidialle tai `av1_amf` AMD:lle).

---

## 1. Esivaatimukset peli-PC:llä (Windows)

1. **Python 3.10+**:
   - Asenna viralliselta sivulta [python.org](https://www.python.org/) tai Windows Storesta (muista ruksata asennuksessa *"Add Python to PATH"*).

2. **FFmpeg**:
   - Helpoin tapa asentaa Windowsissa on avata PowerShell tai komentokehote ja ajaa:
     ```powershell
     winget install Gyan.FFmpeg
     ```
   - (Tai lataa [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) `ffmpeg-release-full.7z` ja lisää sen `bin`-kansio järjestelmän PATH-polkuun).

3. **Verkkolevyt (SMB-jako)**:
   - Palvelimen jaetut kansiot (esim. `/mnt/dell` ja `/mnt/storage`) kytketään Windowsin verkkolevyiksi (esim. `Z:\` tai `Y:\`) tai käytetään suoraa UNC-polkua `\\192.168.1.100\dell`.

---

## 2. Asennus & Asetukset

1. Kopioi koko `client`-kansio peli-PC:lle haluamaasi paikkaan (esim. `C:\optivmaf-worker`).
2. Kopioi `node_config.example.json` nimelle `node_config.json` (tai käynnistä `start_worker.bat`, joka luo sen automaattisesti).
3. Avaa `node_config.json` tekstieditorilla (esim. Notepad / VS Code):

```json
{
  "master_url": "http://192.168.1.100:6699",
  "node_id": "peli-pc",
  "node_name": "Peli-PC (AMD Ryzen 9)",
  "encoder": "libsvtav1",
  "encoder_speedpreset": 6,
  "film_grain": 8,
  "film_grain_denoise": false,
  "force_10bit": true,
  "temp_dir": "C:\\temp\\optivmaf",
  "path_mapping": {
    "/mnt/dell": "Z:\\dell",
    "/mnt/storage": "\\\\192.168.1.100\\storage"
  },
  "poll_interval_sec": 4,
  "skip_vmaf": false,
  "fixed_icq": 24
}
```

### Keskeiset asetukset:

* **`master_url`**: Palvelimen IP ja portti, jossa Optivmaf pyörii (esim. `http://192.168.1.100:6699`).
* **`encoder`**:
  * `"libsvtav1"` — **CPU-pakkaus**: Paras mahdollinen kuvanlaatu ja tilansäästö, tukee `film_grain`-raetta. Erinomainen nopeilla 8–16-ytimisillä prosessoreilla (Ryzen 7/9, Intel i7/i9). Esiasetus `preset 6` on loistava kompromissi.
  * `"av1_nvenc"` — **Nvidia RTX 40 -sarja**: Huippunopea GPU-pakkaus.
  * `"hevc_nvenc"` — **Nvidia GTX 16 / RTX 20/30 -sarja**: Nopea H.265/HEVC GPU-pakkaus.
* **`temp_dir`**: Paikallisen nopean SSD-levyn kansio väliaikaistiedostoille. Enkoodaus tapahtuu lokaalisti, mikä takaa maksimaalisen nopeuden ja säästää verkkoliikennettä. Vasta valmis video kopioidaan verkkolevylle.
* **`path_mapping`**: Mäppäyssäännöt palvelimen Linux-polkujen ja Windowsin asemien välille. Esimerkiksi `/mnt/dell` kääntyy muotoon `Z:\dell`.

---

## 3. Käynnistys

* Kaksoisklikkaa **`start_worker.bat`** (Windows) tai aja komentoriviltä:
  ```cmd
  python worker.py
  ```
* Worker yhdistää palvelimeen, rekisteröityy näkyviin Optivmafin web-käyttöliittymään ja poimii automaattisesti seuraavan vapaan työn jonosta.

---

## 4. Pelaaminen ja keskeyttäminen

* Kun haluat alkaa pelata tai sammuttaa koneen:
  * Paina vain komentorivillä **`Ctrl + C`**.
  * Worker keskeyttää FFmpegin siististi, poistaa keskeneräisen väliaikaistiedoston ja ilmoittaa palvelimelle vapauttavansa työn takaisin jonoon (`release`).
  * Työ ei korruptoidu eikä jää jumiin, vaan palvelin (tai jokin muu kone) voi jatkaa siitä eteenpäin.
* Voit myös pysäyttää tai laittaa työn tauolle suoraan Optivmafin verkkosivulta (Stop / Pause -painikkeet).
