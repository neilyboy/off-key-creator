# Off-Key Creator - shared image for the `web` and `worker` services.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# System dependencies:
#   ffmpeg           - audio/video processing engine
#   libsndfile1      - soundfile backend for audio-separator
#   fonts-*          - font families selectable for lyric typography
#   git              - required by some pip packages installed from VCS metadata
#   build-essential  - gcc, needed to compile C extensions without wheels (e.g. diffq)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        fonts-dejavu \
        fonts-liberation \
        fonts-noto-core \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# ------------------------------------------------------------------
# PyTorch install - CPU by default.
#
# --- GPU (CUDA 12.x): comment the CPU line and uncomment the CUDA line,
#     then rebuild with `docker compose build --no-cache`. ---
# ------------------------------------------------------------------
RUN pip install torch==2.3.1 torchaudio==2.3.1 --index-url https://download.pytorch.org/whl/cpu
# RUN pip install torch==2.3.1 torchaudio==2.3.1 --index-url https://download.pytorch.org/whl/cu121

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY off-key-creator-logo.png ./app/static/off-key-creator-logo.png

# Default command (web). The worker overrides this in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
