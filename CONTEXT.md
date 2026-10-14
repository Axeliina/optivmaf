# CONTEXT.md

## Yleiskuva
optivmaf on automatisoitu videoiden uudelleenpakkaus- ja optimointijärjestelmä (VMAF-laadunvarmistuksella ja laitteistokiihdytyksellä), jota ohjataan web-käyttöliittymästä. Sovellus ajetaan Dockerilla (`docker compose up -d`) tai käynnistysskriptillä (`./start.sh`), joka käynnistää FastAPI-taustapalvelun ja Worker-enkooderin.

## Teknologiat & Stack
- **Backend:** Python 3.11+, FastAPI, Uvicorn, Pydantic, FFmpeg / libvmaf (Hardware: Intel/AMD VAAPI, NVIDIA NVENC; CPU: SVT-AV1, x265, x264)
- **Tietokanta:** SQLite (`data/optivmaf.db`), SQLAlchemy ORM
- **Web-frontend:** Yhden tiedoston SPA (`web/index.html`), Vanilla JS, Tailwind CSS (CDN)

## Kansiorakenne & Vastuut
- `api`: FastAPI-reitit ja REST API -rajapinnat tilastojen, jononhallinnan, asetusten ja hajautettujen työasemien (`/api/nodes`) ohjaamiseen.
- `core`: Konfiguraation validointi (`config.py`), tietokantamallit ja -yhteys (`database.py`), järjestelmämetriikat (`stats.py`, `sysmon.py`) sekä työasemien tilanhallinta ja vahti (`nodes.py`).
- `workers`: Työjonon orkestrointi (`job_manager.py`), videotiedostojen skannaus (`scanner.py`), mediatietojen luku (`probe.py`) ja FFmpeg-enkoodaus VMAF-testauksineen (`encoder.py`).
- `client`: Peli-PC:lle tai muille lähiverkon koneille tarkoitettu itsenäinen worker-agentti (`worker.py`, `node_config.json`, `start_worker.bat`, `README.md`).
- `data`: Pysyvät tiedostot ja tilatiedot; sisältää `config.json`-konfiguraation ja `optivmaf.db`-tietokannan.
- `web`: Selainkäyttöliittymä (dashboard, jononhallinta, lokit, työasemat ja asetukset yhdessä HTML/JS-tiedostossa).

## Keskeiset säännöt
- **Koodaustyyli:** Selkeä Python-tyyppitys (`typing`, Pydantic-mallit) ja suomenkieliset lokitukset/kommentit koodikannan tyylin mukaisesti.
- **Tietokanta:** Käytä aina `core.database.SessionLocal`-sessiota kontekstimanagerin (`with SessionLocal() as db:`) kautta; älä tee suoria raakoja SQL-kyselyitä SQLite-tiedostoon.
- **Konfiguraatio:** Kaikki asetukset luetaan `core.config.load_config()` -funktion kautta ja validoidaan Pydanticilla; tallennus tapahtuu JSON-muodossa `data/config.json`-tiedostoon.
- **Rajoitukset:** Älä muokkaa `data/optivmaf.db`- tai `data/config.json`-tiedostoja suoraan tiedostojärjestelmästä, älä poista videoita kysymättä, äläkä riko monikansiomäppäyksen polkulogiikkaa (`folder_id:rel_path`).
