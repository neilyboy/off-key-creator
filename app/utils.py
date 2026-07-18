"""Shared helpers: filename sanitization, ID3 metadata, color validation."""
import re
from pathlib import Path

from mutagen import File as MutagenFile

# Characters illegal on common filesystems, stripped from output filenames.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_HEX_COLOR = re.compile(r"^#?[0-9a-fA-F]{6}$")


def sanitize_filename_part(value: str, fallback: str = "Unknown") -> str:
    """Strip illegal filesystem characters and trim a metadata string.

    Used to build the strict `Artist - Title.mp4` output filename.
    """
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    return cleaned or fallback


def validate_hex_color(value: str, fallback: str = "#FFFFFF") -> str:
    """Return a normalized `#RRGGBB` string, or the fallback if invalid.

    Guards FFmpeg/ASS filter strings against injection via color fields.
    """
    if value and _HEX_COLOR.match(value.strip()):
        return "#" + value.strip().lstrip("#").upper()
    return fallback


def extract_id3_metadata(path: Path) -> dict:
    """Best-effort extraction of Artist/Title tags from an audio file."""
    artist, title = "", ""
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            artist = (audio.tags.get("artist") or [""])[0]
            title = (audio.tags.get("title") or [""])[0]
    except Exception:
        pass  # Unreadable/absent tags are fine; the user edits them in the UI.
    if not title:
        title = path.stem
    return {"artist": artist.strip(), "title": title.strip()}


def hex_to_ass_color(hex_color: str, alpha: str = "00") -> str:
    """Convert `#RRGGBB` to the ASS `&HAABBGGRR` color format."""
    h = validate_hex_color(hex_color).lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()
