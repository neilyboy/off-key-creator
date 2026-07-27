"""FastAPI application: UI, uploads, job orchestration, WebSocket progress."""
import asyncio
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_IMAGE_EXTENSIONS,
    FONT_CHOICES,
    REDIS_URL,
    RESOLUTIONS,
    SEPARATION_MODELS,
    UPLOADS_DIR,
    WHISPER_MODELS,
)
from .jobs import job_dir, load_job, new_job_id, save_job, update_job
from .tasks import realign_lyrics, render_video, separate_audio, transcribe_audio
from .utils import extract_id3_metadata

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Off-Key Creator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB


# ----------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------
class MetadataBody(BaseModel):
    artist: str
    title: str


class SeparateBody(BaseModel):
    model: str


class TranscribeBody(BaseModel):
    model: str


class LyricsBody(BaseModel):
    lines: list[str]


class RenderBody(BaseModel):
    settings: dict


def _get_job_or_404(job_id: str) -> dict:
    try:
        return load_job(job_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Job not found")


async def _save_upload(upload: UploadFile, dest: Path) -> None:
    """Stream an upload to disk with a size cap."""
    written = 0
    with open(dest, "wb") as f:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large (max 200 MB)")
            f.write(chunk)


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "separation_models": SEPARATION_MODELS,
            "whisper_models": WHISPER_MODELS,
            "resolutions": list(RESOLUTIONS.keys()),
            "font_choices": FONT_CHOICES,
        },
    )


# ----------------------------------------------------------------------
# 1. Upload & metadata
# ----------------------------------------------------------------------
@app.post("/api/upload")
async def upload_audio(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}",
        )
    job_id = new_job_id()
    dest = UPLOADS_DIR / f"{job_id}{ext}"
    await _save_upload(file, dest)

    meta = extract_id3_metadata(dest)
    job_dir(job_id)  # create working directory
    save_job(job_id, {
        "job_id": job_id,
        "status": "uploaded",
        "upload_path": str(dest),
        "original_filename": file.filename,
        "artist": meta["artist"],
        "title": meta["title"],
    })
    return {"job_id": job_id, "artist": meta["artist"], "title": meta["title"]}


@app.post("/api/jobs/{job_id}/metadata")
async def set_metadata(job_id: str, body: MetadataBody):
    _get_job_or_404(job_id)
    update_job(job_id, artist=body.artist.strip(), title=body.title.strip())
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    return _get_job_or_404(job_id)


# ----------------------------------------------------------------------
# 2. Separation
# ----------------------------------------------------------------------
@app.post("/api/jobs/{job_id}/separate")
async def start_separation(job_id: str, body: SeparateBody):
    _get_job_or_404(job_id)
    if body.model not in SEPARATION_MODELS:
        raise HTTPException(status_code=400, detail="Unknown separation model")
    update_job(job_id, status="separating")
    separate_audio.delay(job_id, body.model)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/audio/{stem}")
async def get_stem(job_id: str, stem: str):
    job = _get_job_or_404(job_id)
    key = {"vocals": "vocals_path", "instrumental": "instrumental_path"}.get(stem)
    if not key:
        raise HTTPException(status_code=404, detail="Unknown stem")
    path = job.get(key)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Stem not ready")
    return FileResponse(path, media_type="audio/flac", filename=f"{stem}.flac")


# ----------------------------------------------------------------------
# 3. Transcription & lyric review checkpoint
# ----------------------------------------------------------------------
@app.post("/api/jobs/{job_id}/transcribe")
async def start_transcription(job_id: str, body: TranscribeBody):
    _get_job_or_404(job_id)
    if body.model not in WHISPER_MODELS:
        raise HTTPException(status_code=400, detail="Unknown Whisper model")
    update_job(job_id, status="transcribing")
    transcribe_audio.delay(job_id, body.model)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/lyrics")
async def get_lyrics(job_id: str):
    job = _get_job_or_404(job_id)
    path = job.get("lyrics_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Lyrics not ready")
    with open(path, "r", encoding="utf-8") as f:
        lyrics = json.load(f)
    lines = [
        {
            # Re-emit duet singer markers so they survive editing round-trips.
            "text": (f"{seg['singer']}: " if seg.get("singer") in (1, 2) else "")
                    + " ".join(w["word"] for w in seg["words"]),
            "start": seg["start"],
            "end": seg["end"],
        }
        for seg in lyrics["segments"]
    ]
    return {"language": lyrics.get("language"), "lines": lines}


@app.post("/api/jobs/{job_id}/lyrics")
async def save_lyrics(job_id: str, body: LyricsBody):
    """Apply user text edits, then re-derive accurate word timings.

    - Same word count on a line: each word keeps its exact timing.
    - Different word count: the line's time span is redistributed across
      the new words, weighted by word length, as a provisional estimate.
    - If any text changed, a background forced-alignment task re-aligns the
      corrected words against the vocal stem for true millisecond timings
      (fixes e.g. "Breeze him" -> "Freezin'" where word counts differ).
    - Lines may be prefixed with "1:" or "2:" to assign a duet singer;
      the marker is stored on the segment, not rendered as lyric text.
    """
    job = _get_job_or_404(job_id)
    path = job.get("lyrics_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Lyrics not ready")
    with open(path, "r", encoding="utf-8") as f:
        lyrics = json.load(f)
    segments = lyrics["segments"]
    if len(body.lines) != len(segments):
        raise HTTPException(
            status_code=400,
            detail=f"Line count must stay at {len(segments)} to keep timing sync "
                   f"(got {len(body.lines)}). Edit words, not line breaks.",
        )

    text_changed = False
    for seg, new_text in zip(segments, body.lines):
        # Duet singer markers: strip a leading "1:" / "2:" and store it.
        marker = re.match(r"^\s*([12])\s*:\s*", new_text)
        if marker:
            seg["singer"] = int(marker.group(1))
            new_text = new_text[marker.end():]
        else:
            seg.pop("singer", None)
        new_words = [w for w in new_text.split() if w]
        old_words = seg["words"]
        if not new_words:
            continue  # keep the original line rather than emptying it
        if new_words != [w["word"] for w in old_words]:
            text_changed = True
        if len(new_words) == len(old_words):
            for old, text in zip(old_words, new_words):
                old["word"] = text
        else:
            start = old_words[0]["start"]
            end = old_words[-1]["end"]
            span = max(end - start, 0.2)
            total_chars = sum(len(w) for w in new_words) or 1
            rebuilt, cursor = [], start
            for w in new_words:
                dur = span * len(w) / total_chars
                rebuilt.append({"word": w, "start": round(cursor, 3),
                                "end": round(cursor + dur, 3)})
                cursor += dur
            rebuilt[-1]["end"] = end
            seg["words"] = rebuilt

    with open(path, "w", encoding="utf-8") as f:
        json.dump(lyrics, f, ensure_ascii=False, indent=2)

    if text_changed:
        update_job(job_id, status="realigning")
        realign_lyrics.delay(job_id)
    return {"ok": True, "realigning": text_changed}


# ----------------------------------------------------------------------
# 4 & 5. Background upload, render, download
# ----------------------------------------------------------------------
@app.post("/api/jobs/{job_id}/background")
async def upload_background(job_id: str, file: UploadFile = File(...)):
    _get_job_or_404(job_id)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported image format '{ext}'")
    dest = job_dir(job_id) / f"background{ext}"
    await _save_upload(file, dest)
    update_job(job_id, background_image_path=str(dest))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/backgrounds")
async def upload_backgrounds(job_id: str, files: list[UploadFile] = File(...)):
    """Upload a set of images for a slideshow background (replaces the set)."""
    _get_job_or_404(job_id)
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400,
                                detail=f"Unsupported image format '{ext}' ({f.filename})")
    slides_dir = job_dir(job_id) / "slideshow"
    if slides_dir.exists():
        shutil.rmtree(slides_dir)
    slides_dir.mkdir(parents=True)
    paths = []
    for idx, f in enumerate(files):
        ext = Path(f.filename or "").suffix.lower()
        dest = slides_dir / f"slide_{idx:03d}{ext}"
        await _save_upload(f, dest)
        paths.append(str(dest))
    update_job(job_id, background_image_paths=paths)
    return {"ok": True, "count": len(paths)}


@app.post("/api/jobs/{job_id}/logo")
async def upload_logo(job_id: str, file: UploadFile = File(...)):
    """Upload a logo/watermark image (PNG with alpha recommended)."""
    _get_job_or_404(job_id)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported image format '{ext}'")
    dest = job_dir(job_id) / f"logo{ext}"
    await _save_upload(file, dest)
    update_job(job_id, logo_image_path=str(dest))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/render")
async def start_render(job_id: str, body: RenderBody):
    job = _get_job_or_404(job_id)
    if not job.get("instrumental_path") or not job.get("lyrics_path"):
        raise HTTPException(status_code=409, detail="Separation and transcription must finish first")
    if body.settings.get("resolution") not in RESOLUTIONS:
        raise HTTPException(status_code=400, detail="Unknown resolution")
    update_job(job_id, status="rendering", render_settings=body.settings)
    render_video.delay(job_id, body.settings)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download")
async def download_video(job_id: str):
    job = _get_job_or_404(job_id)
    path = job.get("output_path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Video not rendered yet")
    return FileResponse(path, media_type="video/mp4", filename=Path(path).name)


# ----------------------------------------------------------------------
# Reference lyrics (LRCLIB) - free, keyless lyric database lookup used as
# a side-by-side cross-reference while reviewing the AI transcription.
# ----------------------------------------------------------------------
def _lrclib_fetch(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "OffKeyCreator/1.0 (https://github.com/neilyboy/off-key-creator)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


@app.get("/api/jobs/{job_id}/reference-lyrics")
async def reference_lyrics(job_id: str):
    job = _get_job_or_404(job_id)
    artist, title = job.get("artist", "").strip(), job.get("title", "").strip()
    if not artist or not title:
        raise HTTPException(status_code=400, detail="Artist and Title metadata are required")

    def lookup():
        # Exact match first, then fall back to fuzzy search.
        exact_url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(
            {"artist_name": artist, "track_name": title}
        )
        data = _lrclib_fetch(exact_url)
        if data and data.get("plainLyrics"):
            return data
        search_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(
            {"q": f"{artist} {title}"}
        )
        results = _lrclib_fetch(search_url) or []
        return next((r for r in results if r.get("plainLyrics")), None)

    try:
        data = await asyncio.to_thread(lookup)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Lyric lookup failed: {exc}")
    if not data:
        raise HTTPException(status_code=404, detail="No lyrics found on LRCLIB for this song")
    return {
        "source": "lrclib.net",
        "matched_artist": data.get("artistName"),
        "matched_title": data.get("trackName"),
        "lyrics": data["plainLyrics"],
    }


# ----------------------------------------------------------------------
# WebSocket: relay Celery progress events from Redis pub/sub
# ----------------------------------------------------------------------
@app.websocket("/ws/{job_id}")
async def progress_ws(websocket: WebSocket, job_id: str):
    await websocket.accept()
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe(f"progress:{job_id}")
    try:
        # Replay the last known event so reconnecting clients catch up.
        try:
            last = load_job(job_id).get("last_event")
            if last:
                await websocket.send_text(json.dumps(last))
        except (FileNotFoundError, ValueError):
            pass

        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg["type"] == "message":
                await websocket.send_text(msg["data"])
            else:
                await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(f"progress:{job_id}")
        await pubsub.close()
        await r.aclose()


@app.exception_handler(500)
async def internal_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})
