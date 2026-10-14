import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.database import init_db
from api.routes import router as api_router, background_scan
from core.config import load_config
from fastapi.responses import FileResponse

# --- Access log -filttteri: piilotetaan tiheästi pollattavat reitit ---
_SILENT_PATHS = {"/api/progress", "/api/queue", "/api/stats", "/api/system", "/api/nodes", "/api/nodes/heartbeat"}

class _SilentPollingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(path in msg for path in _SILENT_PATHS)

logging.getLogger("uvicorn.access").addFilter(_SilentPollingFilter())
# -----------------------------------------------------------------------

async def _auto_scan_loop():
    while True:
        try:
            cfg = load_config()
            interval = cfg.files.auto_scan_interval_minutes
            if interval and interval > 0:
                print(f"[AutoScan] Tarkistetaan kirjasto automaattisesti (väli {interval} min)...")
                # Suoritetaan synkroninen skannaus thread poolissa
                await asyncio.to_thread(background_scan)
                await asyncio.sleep(interval * 60)
            else:
                await asyncio.sleep(60) # Tarkistetaan asetus uudelleen minuutin päästä
        except Exception as e:
            print(f"[AutoScan] Virhe automaattisessa skannauksessa: {e}")
            await asyncio.sleep(60)

async def _node_watchdog_loop():
    """Tarkistaa 10s välein katkenneet ulkoiset työasemat ja palauttaa niiden työt jonoon."""
    from core.nodes import recover_stale_nodes
    while True:
        try:
            recover_stale_nodes(timeout_sec=60.0)
        except Exception as e:
            print(f"[NodeWatchdog] Virhe tarkistuksessa: {e}")
        await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ajetaan käynnistyksen yhteydessä
    print("Alustetaan tietokanta...")
    init_db()
    
    # Käynnistetään taustaluupit
    auto_scan_task = asyncio.create_task(_auto_scan_loop())
    node_watchdog_task = asyncio.create_task(_node_watchdog_loop())

    # Nollataan automaattisesti kaikki PROCESSING-tilassa olevat työt.
    # Jos API/kontti on käynnistetty uudelleen kesken enkoodauksen,
    # ne jäävät muuten jumiin eikä enkoodausta voida jatkaa.
    from core.database import SessionLocal, VideoRecord, JobStatus
    _session = SessionLocal()
    try:
        stuck = _session.query(VideoRecord).filter(VideoRecord.status == JobStatus.PROCESSING).all()
        if stuck:
            print(f"[Startup] Nollataan {len(stuck)} jumissa olevaa PROCESSING-työtä → QUEUED")
            for j in stuck:
                j.status = JobStatus.QUEUED
                j.notes = (j.notes or "") + " | Auto-nollattu (API restart)"
            _session.commit()
    finally:
        _session.close()
    yield
    # Tähän tulisi sammutuskoodi tarvittaessa
    auto_scan_task.cancel()
    node_watchdog_task.cancel()
    print("Sammutetaan API...")

app = FastAPI(
    title="optivmaf API",
    description="Kevyt videokirjaston automaattisen enkoodauksen hallinta",
    version="0.1.0",
    lifespan=lifespan
)

# Salli pyynnöt UI:sta (Säädä UI_ORIGIN myöhemmin tuotannossa)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Liitetään reitit
app.include_router(api_router, prefix="/api")

@app.get("/")
def serve_ui():
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    # Aja komennolla: uvicorn api.main:app --reload --port 8000
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True, access_log=True)

