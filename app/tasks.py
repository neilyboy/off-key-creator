"""Celery tasks: vocal separation, transcription/alignment, and video render.

Every task publishes progress events over Redis pub/sub (see app.jobs) so
the FastAPI WebSocket relay can stream live updates to the browser. All
tasks are wrapped so that any exception is pushed to the UI as a terminal
"error" event instead of leaving the user hanging.
"""
import json
import shutil
import subprocess
import traceback
from pathlib import Path

import ffmpeg

from .celery_app import celery_app
from .config import (
    DEVICE,
    MODELS_DIR,
    OUTPUT_DIR,
    RESOLUTIONS,
    SEPARATION_MODELS,
    VISUALIZER_TYPES,
    WHISPER_MODELS,
)
from .ass_builder import build_ass
from .jobs import job_dir, load_job, publish_progress, update_job
from .utils import sanitize_filename_part, validate_hex_color

FPS = 30


def _fail(job_id: str, stage: str, exc: Exception) -> None:
    """Publish a terminal error event with a human-readable message."""
    msg = f"{type(exc).__name__}: {exc}"
    traceback.print_exc()
    try:
        update_job(job_id, status="error", error=msg)
    except Exception:
        pass
    publish_progress(job_id, stage, 100, status="error", message=msg)


# ======================================================================
# 1. Vocal separation (audio-separator / Roformer models)
# ======================================================================
@celery_app.task(name="app.tasks.separate_audio")
def separate_audio(job_id: str, model_key: str) -> dict:
    stage = "separation"
    try:
        if model_key not in SEPARATION_MODELS:
            raise ValueError(f"Unknown separation model: {model_key}")
        job = load_job(job_id)
        input_path = Path(job["upload_path"])
        if not input_path.exists():
            raise FileNotFoundError("Uploaded audio file is missing")
        work_dir = job_dir(job_id)
        model_filename = SEPARATION_MODELS[model_key]["filename"]

        publish_progress(job_id, stage, 5, message="Loading separation model (downloads on first run)...")

        # Imported lazily so the web container never loads torch.
        from audio_separator.separator import Separator

        separator = Separator(
            model_file_dir=str(MODELS_DIR / "audio-separator"),
            output_dir=str(work_dir),
            output_format="FLAC",
        )
        separator.load_model(model_filename=model_filename)

        publish_progress(job_id, stage, 30, message="Separating vocals and instrumental...")
        output_files = separator.separate(str(input_path))

        publish_progress(job_id, stage, 90, message="Finalizing stems...")

        vocals_path = work_dir / "vocals.flac"
        instrumental_path = work_dir / "instrumental.flac"
        for name in output_files:
            p = Path(name)
            if not p.is_absolute():
                p = work_dir / p
            lowered = p.name.lower()
            if "vocal" in lowered and "instrument" not in lowered:
                shutil.move(str(p), str(vocals_path))
            else:  # "(Instrumental)" / "(Other)" stem
                shutil.move(str(p), str(instrumental_path))

        if not vocals_path.exists() or not instrumental_path.exists():
            raise RuntimeError("Separation did not produce both stems")

        update_job(
            job_id,
            status="separated",
            separation_model=model_key,
            vocals_path=str(vocals_path),
            instrumental_path=str(instrumental_path),
        )
        publish_progress(job_id, stage, 100, status="done", message="Separation complete")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - must surface everything to the UI
        _fail(job_id, stage, exc)
        raise


# ======================================================================
# 2. Transcription + forced alignment (whisperX)
# ======================================================================
def _fill_missing_word_times(words: list, seg_start: float, seg_end: float) -> list:
    """Interpolate timings for words whisperX could not align (e.g. digits)."""
    n = len(words)
    for i, w in enumerate(words):
        if w.get("start") is None or w.get("end") is None:
            prev_end = next(
                (words[j]["end"] for j in range(i - 1, -1, -1) if words[j].get("end") is not None),
                seg_start,
            )
            next_start = next(
                (words[j]["start"] for j in range(i + 1, n) if words[j].get("start") is not None),
                seg_end,
            )
            span = max(next_start - prev_end, 0.1)
            w["start"] = prev_end
            w["end"] = prev_end + span / 2
    return words


@celery_app.task(name="app.tasks.transcribe_audio")
def transcribe_audio(job_id: str, whisper_model: str) -> dict:
    stage = "transcription"
    try:
        if whisper_model not in WHISPER_MODELS:
            raise ValueError(f"Unknown Whisper model: {whisper_model}")
        job = load_job(job_id)
        vocals_path = job.get("vocals_path")
        if not vocals_path or not Path(vocals_path).exists():
            raise FileNotFoundError("Vocal stem not found - run separation first")

        publish_progress(job_id, stage, 5, message=f"Loading Whisper '{whisper_model}' (downloads on first run)...")

        import whisperx

        compute_type = "float16" if DEVICE == "cuda" else "int8"
        model = whisperx.load_model(
            whisper_model,
            DEVICE,
            compute_type=compute_type,
            download_root=str(MODELS_DIR / "whisper"),
        )

        publish_progress(job_id, stage, 25, message="Transcribing vocal track...")
        audio = whisperx.load_audio(vocals_path)
        result = model.transcribe(audio, batch_size=8)
        language = result["language"]

        publish_progress(job_id, stage, 60, message="Loading alignment model...")
        align_model, align_metadata = whisperx.load_align_model(
            language_code=language, device=DEVICE
        )

        publish_progress(job_id, stage, 75, message="Force-aligning words (millisecond timing)...")
        aligned = whisperx.align(
            result["segments"], align_model, align_metadata, audio, DEVICE,
            return_char_alignments=False,
        )

        segments = []
        for seg in aligned["segments"]:
            words = [
                {"word": w.get("word", "").strip(),
                 "start": w.get("start"),
                 "end": w.get("end")}
                for w in seg.get("words", [])
                if w.get("word", "").strip()
            ]
            if not words:
                continue
            words = _fill_missing_word_times(
                words, float(seg["start"]), float(seg["end"])
            )
            segments.append({
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "words": words,
            })

        lyrics_path = job_dir(job_id) / "lyrics.json"
        with open(lyrics_path, "w", encoding="utf-8") as f:
            json.dump({"language": language, "segments": segments}, f,
                      ensure_ascii=False, indent=2)

        update_job(
            job_id,
            status="transcribed",
            whisper_model=whisper_model,
            language=language,
            lyrics_path=str(lyrics_path),
        )
        publish_progress(job_id, stage, 100, status="done",
                         message=f"Transcribed {len(segments)} lines")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, stage, exc)
        raise


# ======================================================================
# 3. Final video render (FFmpeg layering + live progress)
# ======================================================================
def _build_ffmpeg_command(job: dict, settings: dict, work_dir: Path,
                          ass_path: Path, out_path: Path, duration: float) -> list:
    """Construct the FFmpeg arg list (no shell => no injection surface).

    Layers, bottom to top:
      background color/image -> optional visualizer (with opacity) -> ASS subs
    Audio: the isolated instrumental stem.
    """
    width, height = RESOLUTIONS[settings["resolution"]]
    instrumental = job["instrumental_path"]

    # --- Background layer ---
    background = settings.get("background", {})
    bg_image = job.get("background_image_path")
    if background.get("type") == "image" and bg_image and Path(bg_image).exists():
        bg = (
            ffmpeg.input(bg_image, loop=1, framerate=FPS)
            .filter("scale", width, height, force_original_aspect_ratio="increase")
            .filter("crop", width, height)
            .filter("setsar", 1)
        )
    else:
        color = validate_hex_color(background.get("color", "#000000"), "#000000")
        bg = ffmpeg.input(
            f"color=c=0x{color.lstrip('#')}:s={width}x{height}:r={FPS}", f="lavfi"
        )

    video = bg

    # --- Visualizer layer (middle) ---
    vis_cfg = settings.get("visualizer", {})
    if vis_cfg.get("enabled"):
        vis_type = vis_cfg.get("type", "showwaves")
        if vis_type not in VISUALIZER_TYPES:
            vis_type = "showwaves"
        vis_color = validate_hex_color(vis_cfg.get("color", "#FFFFFF")).lstrip("#")
        opacity = min(max(float(vis_cfg.get("opacity", 1.0)), 0.0), 1.0)
        vis_h = height // 3

        vis_audio = ffmpeg.input(instrumental).audio
        if vis_type == "showwaves":
            vis = vis_audio.filter(
                "showwaves", s=f"{width}x{vis_h}", mode="cline",
                rate=FPS, colors=f"0x{vis_color}",
            )
        else:
            vis = vis_audio.filter(
                "showfreqs", s=f"{width}x{vis_h}", mode="bar",
                fscale="log", colors=f"0x{vis_color}",
            ).filter("fps", FPS)
        vis = vis.filter("format", "rgba").filter("colorchannelmixer", aa=opacity)
        video = ffmpeg.overlay(
            video, vis, x="(main_w-overlay_w)/2", y="(main_h-overlay_h)/2",
            eof_action="pass",
        )

    # --- Subtitle layer (top) ---
    video = video.filter("ass", str(ass_path))

    # --- Audio: instrumental stem (separate input from the visualizer's) ---
    out_audio = ffmpeg.input(instrumental).audio

    stream = ffmpeg.output(
        video, out_audio, str(out_path),
        vcodec="libx264", preset="medium", pix_fmt="yuv420p", r=FPS,
        acodec="aac", audio_bitrate="320k",
        t=duration, movflags="+faststart",
    ).overwrite_output().global_args(
        "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"
    )
    return stream.compile()


@celery_app.task(name="app.tasks.render_video")
def render_video(job_id: str, settings: dict) -> dict:
    stage = "render"
    try:
        job = load_job(job_id)
        work_dir = job_dir(job_id)

        for key in ("instrumental_path", "lyrics_path"):
            if not job.get(key) or not Path(job[key]).exists():
                raise FileNotFoundError(f"Missing prerequisite: {key}")
        if settings.get("resolution") not in RESOLUTIONS:
            raise ValueError(f"Unknown resolution: {settings.get('resolution')}")

        publish_progress(job_id, stage, 2, message="Building karaoke subtitles...")

        with open(job["lyrics_path"], "r", encoding="utf-8") as f:
            lyrics = json.load(f)
        width, height = RESOLUTIONS[settings["resolution"]]
        subs = settings.get("subtitles", {})
        ass_text = build_ass(
            lyrics["segments"], width, height,
            text_color=validate_hex_color(subs.get("text_color", "#FFFFFF")),
            highlight_color=validate_hex_color(subs.get("highlight_color", "#00A5FF")),
        )
        ass_path = work_dir / "subtitles.ass"
        ass_path.write_text(ass_text, encoding="utf-8")

        duration = float(ffmpeg.probe(job["instrumental_path"])["format"]["duration"])
        tmp_out = work_dir / "render.mp4"
        cmd = _build_ffmpeg_command(job, settings, work_dir, ass_path, tmp_out, duration)

        publish_progress(job_id, stage, 5, message="Rendering video with FFmpeg...")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        last_pct = 5.0
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                try:
                    rendered_s = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                pct = 5 + min(rendered_s / duration, 1.0) * 93
                if pct - last_pct >= 1:
                    last_pct = pct
                    publish_progress(
                        job_id, stage, pct,
                        message=f"Rendering... {rendered_s:.0f}s / {duration:.0f}s",
                    )
        proc.stdout.close()
        stderr_tail = proc.stderr.read()[-2000:]
        proc.stderr.close()
        if proc.wait() != 0:
            raise RuntimeError(f"FFmpeg failed: {stderr_tail.strip() or 'unknown error'}")

        # --- Strict `Artist - Title.mp4` naming into ./data/output ---
        artist = sanitize_filename_part(job.get("artist", ""), "Unknown Artist")
        title = sanitize_filename_part(job.get("title", ""), "Unknown Title")
        final_path = OUTPUT_DIR / f"{artist} - {title}.mp4"
        shutil.move(str(tmp_out), str(final_path))

        update_job(job_id, status="rendered", output_path=str(final_path),
                   output_filename=final_path.name)
        publish_progress(job_id, stage, 100, status="done",
                         message=f"Saved {final_path.name}")
        return {"ok": True, "output": str(final_path)}
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, stage, exc)
        raise
