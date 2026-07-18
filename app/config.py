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
# Vocal separation models.
#
# Registry models: filenames from the audio-separator / UVR model zoo
# (verify with `audio-separator --list_models`).
#
# Custom models: not in the registry, downloaded directly from Hugging
# Face via the `custom_download` entry (see CustomModelSeparator in
# app/tasks.py). The yaml filename MUST contain "roformer" so
# audio-separator applies its Roformer loading path.
#
# Note: unwa's "BS-Roformer HyperACE" uses a modified architecture
# (extra SegmModel/HyperACE branch) that audio-separator's stock
# BS-Roformer implementation cannot load, so the closest supported
# unwa vocal model (Vocals Revive V3e) is offered instead.
# ------------------------------------------------------------------
SEPARATION_MODELS = {
    "mel-roformer-deux": {
        "label": "Mel-Roformer Deux by becruily",
        "filename": "mel_band_roformer_deux_becruily.ckpt",
        "custom_download": {
            "ckpt_url": "https://huggingface.co/becruily/mel-band-roformer-deux/resolve/main/becruily_deux.ckpt",
            "yaml_url": "https://huggingface.co/becruily/mel-band-roformer-deux/resolve/main/config_deux_becruily.yaml",
            "yaml_filename": "config_mel_band_roformer_deux_becruily.yaml",
        },
    },
    "bs-roformer-revive": {
        "label": "BS-Roformer Vocals Revive V3e by unwa",
        "filename": "bs_roformer_vocals_revive_v3e_unwa.ckpt",
    },
    "bs-roformer-resurrection": {
        "label": "BS-Roformer Resurrection by unwa",
        "filename": "bs_roformer_instrumental_resurrection_unwa.ckpt",
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
