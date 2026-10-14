FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

# ── Layer 1: Järjestelmäpaketit ────────────────────────────────────────────────
# --no-install-recommends pitää imagen pienenä ja välimuisti pysyy vakaana
# kun pakettilistaa ei muuteta.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    xz-utils \
    libva-drm2 \
    libva2 \
    intel-media-va-driver-non-free \
    mesa-va-drivers \
    vainfo \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: Staattinen FFmpeg VMAF-laskentaa varten ──────────────────────────
# Käytetään "latest"-tagia, jotta tiedostonimi pysyy ennustettavana.
# Välimuistin voi rikkoa manuaalisesti (jotta uusin versio ladataan) muuttamalla tätä arvoa:
# docker compose build --build-arg FFMPEG_CACHEBUST=$(date +%s)
ARG FFMPEG_CACHEBUST=1
ARG FFMPEG_VMAF_FILE=ffmpeg-master-latest-linux64-gpl
RUN wget -q "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/${FFMPEG_VMAF_FILE}.tar.xz" && \
    tar -xf "${FFMPEG_VMAF_FILE}.tar.xz" && \
    cp "${FFMPEG_VMAF_FILE}/bin/ffmpeg" /usr/local/bin/ffmpeg-vmaf && \
    rm -rf "${FFMPEG_VMAF_FILE}"*

# ── Layer 3: Python-virtuaaliympäristö ────────────────────────────────────────
WORKDIR /app
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ── Layer 4: Python-riippuvuudet (cachetaan erikseen koodista) ────────────────
# VAIN requirements.txt kopioidaan ensin → tämä layer buildataan uudelleen
# vain kun requirements.txt muuttuu, ei joka kerta kun koodia muutetaan.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Layer 5: Sovelluksen lähdekoodi ───────────────────────────────────────────
# Tämä on ainoa layer joka muuttuu normaalin kehitystyön aikana.
COPY . .
RUN chmod +x start.sh

EXPOSE 6699
CMD ["./start.sh"]
