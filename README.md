# OptiVMAF 🎬⚡

Automaattinen, älykäs videoiden optimointi- ja uudelleenpakkausjärjestelmä reaaliaikaisella **VMAF**-laadunvarmistuksella, monen kansion tuella ja modernilla web-käyttöliittymällä.

OptiVMAF ei vain pakkaa videoita kiinteällä laatuarvolla, vaan se testaa automaattisesti näyteklippejä ja haarukoi optimaalisen laatuarvon (ICQ/CQ/CRF), jotta saavutetaan haluttu visuaalinen laatu (esim. VMAF 93) pienimmällä mahdollisella tiedostokoolla.

Tukee laitteistokiihdytystä (**Intel Arc / iGPU VAAPI**, **NVIDIA NVENC**, **AMD Radeon VAAPI**) sekä korkealaatuisia CPU-enkoodereita (**SVT-AV1**, **x265**, **x264**).

---

## 🌟 Ominaisuudet

- **Älykäs VMAF-haarukointi:** Etsii pienimmän bittinopeuden / korkeimman laatuarvon, jolla saavutetaan haluttu tavoite-VMAF (esim. 93 pistettä = käytännössä häviötön laatu).
- **Hajautettu koodaus (Distributed Worker Nodes):** Voit valjastaa verkossa olevat tietokoneet (esim. Windows/Linux -pelikone) auttamaan enkoodauksessa `client/worker.py`-työkalulla.
- **Moderni Web UI kahdella tilalla:**
  - **Perustila (Easy Mode):** Yksinkertainen näkymä tärkeimmille valinnoille (enkooderi, laatu, ääniasetukset, kansiot).
  - **Lisäasetukset (Advanced Mode):** Kattavat parametrit FFmpegille, GOP-pituudelle, B-kehyksille, film grain -synteesille jne.
- **Enkooderikohtaiset nopeusasetukset:** Omat, itsenäiset presetit eri enkoodereille (`preset_vaapi`, `preset_nvenc`, `preset_svtav1`).
- **Laitteistokiihdytys ja CPU-tuki:**
  - **Intel Arc & Core iGPU:** VAAPI (`av1_vaapi`, `hevc_vaapi`, `h264_vaapi`)
  - **NVIDIA GeForce / RTX:** NVENC (`av1_nvenc`, `hevc_nvenc`, `h264_nvenc`)
  - **AMD Radeon:** VAAPI (`av1_vaapi`, `hevc_vaapi`, `h264_vaapi`)
  - **CPU:** SVT-AV1 (`libsvtav1`), x265 (`libx265`), x264 (`libx264`)
- **Älykäs audio & tekstitys:** Säilyttää halutut kielet, kopioi monikanavaäänet (TrueHD, DTS, EAC3) ja pakkaa stereoraidat valinnaisesti tilaa säästävään muotoon (AAC/Opus). Mahdollisuus luoda stereodownmix automaattisesti.
- **Turvaominaisuudet:** Levytilan ennakkotarkistus, keston poikkeamavertailu (`max_duration_diff`), virheiden automaattinen keskeytys ja epäonnistuneiden turvallinen säilytys.
- **Reaaliaikainen järjestelmämonitorointi:** Seuraa CPU-, RAM-, levy- ja GPU-kuormitusta suoraan dashboardilta.

---

## 🚀 Pika-aloitus (Docker Compose)

### 1. Kloonaa repositorio
```bash
git clone https://github.com/Axeliina/optivmaf.git
cd optivmaf
```

### 2. Määritä ympäristö ja kansiot
Kopioi esimerkkikonfiguraatiot:
```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
cp data/config.example.json data/config.json
```

Muokkaa `.env`-tiedostoon haluamasi kansiopolut:
```env
PORT=6699
MEDIA_INPUT=/polku/lahdekansioon
MEDIA_OUTPUT=/polku/valmiisiin
MEDIA_TEMP=/polku/valiaikaiskansioon
```

### 3. Laitteistokiihdytyksen käyttöönotto

#### Intel tai AMD (VAAPI):
Varmista, että `docker-compose.yml`:ssä on `/dev/dri`-läpivienti:
```yaml
devices:
  - /dev/dri:/dev/dri
```

#### NVIDIA (NVENC):
Asenna isäntäkoneelle [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) ja ota käyttöön `docker-compose.yml`:ssä:
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu, video]
```

### 4. Käynnistä palvelu
```bash
docker compose up -d --build
```

Avaa selain osoitteessa: `http://localhost:6699`

---

## 🖥️ Etäworkerit (Peli-PC / toissijaiset koneet)

Voit kytkeä toisen tietokoneen (Windows tai Linux) auttamaan koodauksessa:
Katso tarkemmat ohjeet tiedostosta [client/README.md](client/README.md).

```bash
cd client
cp node_config.example.json node_config.json
# Muokkaa node_config.json vastaamaan verkkolevyjäsi ja palvelimen osoitetta
python worker.py
```

---

## ⚙️ Asetukset

Asetuksia voidaan muokata suoraan Web UI:n **Asetukset**-välilehdeltä tai tiedostosta `data/config.json`.

| Asetus | Selitys | Oletus |
|---|---|---|
| `video.encoder` | Käytettävä enkooderi (`av1_vaapi`, `av1_nvenc`, `libsvtav1`) | `av1_vaapi` |
| `video.preset_vaapi` | VAAPI-enkoodausnopeus (0–7, 0=hitain/paras laatu, 1=suositus) | `1` |
| `video.preset_svtav1` | SVT-AV1 CPU-nopeus (0–13, 6–8=suositus CPU:lle) | `6` |
| `video.preset_nvenc` | NVENC-nopeus (1–7 eli p1–p7, p7=paras laatu) | `5` |
| `video.max_concurrent_jobs` | Samanaikaisesti enkoodattavien töiden määrä | `1` |
| `video.target_vmaf` | Tavoiteltu VMAF-pistemäärä | `93.0` |
| `video.force_10bit` | Pakota 10-bittinen enkoodaus (eliminoi banding-väriliukumat) | `true` |
| `video.vmaf_use_ramdisk` | Käytä `/dev/shm` RAM-levyä VMAF-testauksessa | `false` |

---

## 📄 Lisenssi
Tämä projekti on julkaistu [MIT-lisenssin](LICENSE) alaisuudessa.
