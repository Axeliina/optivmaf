import enum
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, String, Integer, Float, DateTime, Enum, Text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column

from sqlalchemy import event

# Tietokannan sijainti (voit siirtää tämän myöhemmin config.py:hyn)
DB_PATH = Path("./data/optivmaf.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# Määritetään SQLite-moottori. check_same_thread=False tarvitaan FastAPI:n ja rinnakkaisten säikeiden kanssa.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30}
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

class JobStatus(str, enum.Enum):
    PENDING = "pending"       # Skannattu, odottaa käyttäjän hyväksyntää
    QUEUED = "queued"         # Käyttäjä hyväksynyt enkoodaukseen
    PROCESSING = "processing" # Enkoodaus käynnissä
    COMPLETED = "completed"   # Onnistuneesti pakattu ja valmis
    APPLIED = "applied"       # Käyttäjä on korvannut alkuperäisen tällä
    SKIPPED = "skipped"       # Ohitettu (esim. jo AV1 tai säästö liian pieni)
    FAILED = "failed"         # Virhe prosessissa

class VideoRecord(Base):
    __tablename__ = "video_records"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # Kansio, johon tiedosto kuuluu (esim. "oletus", "elokuvat")
    folder_id: Mapped[str] = mapped_column(String, default="oletus", index=True)

    # Tiedostopolku tallennetaan muodossa "folder_id:rel_path_str", jotta kirjaston
    # juurikansion siirtäminen ei riko tietokantaa ja samannimiset tiedostot eri 
    # kansioissa pysyvät uniikkeina.
    file_path: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.PENDING, index=True)

    # Alkuperäisen tiedoston metatiedot skannauksesta
    orig_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    orig_codec: Mapped[str] = mapped_column(String, nullable=False)
    orig_height: Mapped[int] = mapped_column(Integer, nullable=False)

    # Tulokset (päivitetään kun valmis)
    new_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_time_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    encoder_used: Mapped[str | None] = mapped_column(String, nullable=True)
    vmaf_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Levyn ja RAM-muistin kirjoitustilastot
    encode_bytes_written: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    vmaf_bytes_written: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    vmaf_storage_type: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    # Virheilmoitukset tai ohituksen syyt ("Jo AV1-muodossa")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Työasema/node, joka käsittelee tai käsitteli työn (esim. "Palvelin", "Peli-PC")
    worker_node: Mapped[str | None] = mapped_column(String, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    @property
    def saved_bytes(self) -> int:
        """Laskee säästetyn tilan. Palauttaa 0 jos ei vielä pakattu."""
        if self.status in (JobStatus.COMPLETED, JobStatus.APPLIED) and self.new_size_bytes is not None:
            return self.orig_size_bytes - self.new_size_bytes
        return 0

    @property
    def saved_percent(self) -> float:
        """Laskee säästetyn tilan prosentteina."""
        if self.orig_size_bytes > 0 and self.saved_bytes > 0:
            return (self.saved_bytes / self.orig_size_bytes) * 100.0
        return 0.0

import time
def init_db():
    """Luo tietokannan taulut, jos niitä ei ole, ja varmistaa sarakkeiden migraation."""
    # Yritetään useamman kerran, jotta SQLite ei kaadu lukkiutumiseen
    for attempt in range(10):
        try:
            Base.metadata.create_all(bind=engine)
            break
        except Exception as e:
            if attempt == 9: raise e
            time.sleep(0.5)
            
    for attempt in range(10):
        try:
            with engine.connect() as conn:
                cursor = conn.connection.cursor()
                cursor.execute("PRAGMA table_info(video_records)")
                existing_cols = {row[1] for row in cursor.fetchall()}
                
                if "encode_bytes_written" not in existing_cols:
                    cursor.execute("ALTER TABLE video_records ADD COLUMN encode_bytes_written INTEGER DEFAULT 0")
                if "vmaf_bytes_written" not in existing_cols:
                    cursor.execute("ALTER TABLE video_records ADD COLUMN vmaf_bytes_written INTEGER DEFAULT 0")
                if "vmaf_storage_type" not in existing_cols:
                    cursor.execute("ALTER TABLE video_records ADD COLUMN vmaf_storage_type VARCHAR DEFAULT NULL")
                if "error_log" not in existing_cols:
                    cursor.execute("ALTER TABLE video_records ADD COLUMN error_log TEXT DEFAULT NULL")
                if "folder_id" not in existing_cols:
                    cursor.execute("ALTER TABLE video_records ADD COLUMN folder_id VARCHAR DEFAULT 'oletus'")
                if "worker_node" not in existing_cols:
                    cursor.execute("ALTER TABLE video_records ADD COLUMN worker_node VARCHAR DEFAULT NULL")
                    
                # Migraatio: vanhojen tietueiden file_path ei sisällä folder_id:tä (kaksoispistettä).
                # Lisätään niihin "oletus:" etuliite, jotta uniikkius säilyy ja uusi skanneri ymmärtää ne.
                # Tämä tehdään vain riveille joilla ei vielä ole kaksoispistettä, paitsi jos kyseessä on asemakirjain Windowsissa (C:\...).
                # Optivmaf ajetaan Linux-kontissa, joten polut eivät ala "C:".
                cursor.execute("UPDATE video_records SET file_path = 'oletus:' || file_path WHERE file_path NOT LIKE '%:%'")
                
                conn.connection.commit()
            break
        except Exception as e:
            if attempt == 9:
                try:
                    Path("data").mkdir(parents=True, exist_ok=True)
                    with open("data/crash.log", "a", encoding="utf-8") as f:
                        import traceback
                        f.write("\n--- Database init error ---\n" + traceback.format_exc())
                except Exception:
                    pass
                raise e
            time.sleep(1.0)

    # Alustetaan myös levykirjoitusten kumulatiiviset tilastot
    from core.stats import init_io_stats_from_db
    db = SessionLocal()
    try:
        init_io_stats_from_db(db)
    finally:
        db.close()

def get_db():
    """FastAPI dependency tietokantasession hallintaan."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
