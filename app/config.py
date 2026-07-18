"""Central configuration: paths, model registries, and constants."""
import os
from pathlib import Path

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
DEVICE = os.environ.get("DEVICE", "cpu")  # "cpu" or "cuda"

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
UPLOADS_DIR = DATA_DIR / "uploads"
MODELS_DIR = DATA_DIR / "models"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = DATA_DIR / "output"

for _d in (UPLOADS_DIR, MODELS_DIR, PROCESSED_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a"}
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# ------------------------------------------------------------------
# Vocal separation models (audio-separator / UVR model zoo filenames).
# Verify available filenames with:  audio-separator --list_models
# ------------------------------------------------------------------
SEPARATION_MODELS = {
    "mel-roformer-deux": {
        "label": "Mel-Roformer Deux by becruily",
        "filename": "mel_band_roformer_instrumental_deux_becruily.ckpt",
    },
    "bs-roformer-hyperace": {
        "label": "BS-Roformer HyperACE by unwa",
        "filename": "bs_roformer_vocals_hyperace_unwa.ckpt",
    },
    "bs-roformer-resurrection": {
        "label": "BS-Roformer Resurrection by unwa",
        "filename": "bs_roformer_resurrection_unwa.ckpt",
    },
}

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3"]

RESOLUTIONS = {
    "4K": (3840, 2160),
    "2K": (2560, 1440),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (854, 480),
}

VISUALIZER_TYPES = ["showwaves", "showfreqs"]
