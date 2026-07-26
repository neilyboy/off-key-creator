"""Celery tasks: vocal separation, transcription/alignment, and video render.

Every task publishes progress events over Redis pub/sub (see app.jobs) so
the FastAPI WebSocket relay can stream live updates to the browser. All
tasks are wrapped so that any exception is pushed to the UI as a terminal
"error" event instead of leaving the user hanging.
"""
import contextlib
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import traceback
from collections import Counter
from pathlib import Path

import ffmpeg

from .celery_app import celery_app
from .config import (
    DEVICE,
    FONT_CHOICES,
    MODELS_DIR,
    OUTPUT_DIR,
    RESOLUTIONS,
    SEPARATION_MODELS,
    VISUALIZER_TYPES,
    WHISPER_MODELS,
)
from .ass_builder import build_ass, make_title_text
from .jobs import job_dir, load_job, publish_progress, update_job
from .utils import sanitize_filename_part, validate_hex_color

FPS = 30

# Percentage patterns emitted by tqdm (" 42%|####      | ...") and by
# whisperX's print_progress ("Progress: 42.00%...").
_PCT_PATTERNS = [
    re.compile(r"(\d{1,3}(?:\.\d+)?)%\|"),
    re.compile(r"Progress:\s*(\d{1,3}(?:\.\d+)?)%"),
]


class _ProgressCapture(io.TextIOBase):
    """Tee-style stream wrapper that extracts percentages from tqdm /
    whisperX progress output and forwards them to a callback, while still
    passing everything through to the original stream for docker logs.

    Used with contextlib.redirect_stderr/redirect_stdout around library
    calls that render their own progress bars - this is what turns the
    worker's internal chunk-by-chunk progress into live UI updates.
    """

    def __init__(self, on_percent, passthrough=None):
        self._on_percent = on_percent
        self._passthrough = passthrough

    def write(self, s):
        if self._passthrough is not None:
            try:
                self._passthrough.write(s)
            except Exception:
                pass
        for pattern in _PCT_PATTERNS:
            matches = pattern.findall(s)
            if matches:
                try:
                    self._on_percent(min(float(matches[-1]), 100.0))
                except Exception:
                    pass  # progress reporting must never break the task
        return len(s)

    def flush(self):
        if self._passthrough is not None:
            try:
                self._passthrough.flush()
            except Exception:
                pass


def _stage_mapper(job_id: str, stage: str, lo: float, hi: float, message: str):
    """Return a callback mapping a library's 0-100% onto [lo, hi] overall
    stage progress, throttled to >=1 point steps to limit pub/sub chatter."""
    last = {"value": -1.0}

    def on_percent(pct: float):
        mapped = lo + (hi - lo) * pct / 100.0
        if mapped - last["value"] >= 1.0:
            last["value"] = mapped
            publish_progress(job_id, stage, mapped, message=message)

    return on_percent


def _free_gpu() -> None:
    """Release cached CUDA memory so the next task in this worker process
    (solo pool = one shared process) can allocate VRAM. PyTorch caches
    freed memory instead of returning it to the driver, which starves
    ctranslate2 / other allocators."""
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass


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

        class CustomModelSeparator(Separator):
            """Extends audio-separator to load Roformer models hosted on
            Hugging Face that are absent from the built-in model registry.

            Models flagged with `custom_download` in SEPARATION_MODELS have
            their checkpoint + yaml config fetched directly; everything else
            falls through to the stock registry lookup.
            """

            def download_model_files(self, model_filename):
                custom = next(
                    (m["custom_download"] for m in SEPARATION_MODELS.values()
                     if m["filename"] == model_filename and "custom_download" in m),
                    None,
                )
                if custom is None:
                    return super().download_model_files(model_filename)

                model_path = os.path.join(self.model_file_dir, model_filename)
                yaml_path = os.path.join(self.model_file_dir, custom["yaml_filename"])
                self.download_file_if_not_exists(custom["ckpt_url"], model_path)
                self.download_file_if_not_exists(custom["yaml_url"], yaml_path)
                # Return shape matches the parent: MDXC is the arch used for
                # all Roformer checkpoints with a yaml config.
                return model_filename, "MDXC", model_filename, model_path, custom["yaml_filename"]

        separator = CustomModelSeparator(
            model_file_dir=str(MODELS_DIR / "audio-separator"),
            output_dir=str(work_dir),
            output_format="FLAC",
        )
        # Model download renders a tqdm bar on stderr -> relay as 5-28%.
        load_capture = _ProgressCapture(
            _stage_mapper(job_id, stage, 5, 28, "Downloading / loading model..."),
            passthrough=sys.stderr,
        )
        with contextlib.redirect_stderr(load_capture):
            separator.load_model(model_filename=model_filename)

        publish_progress(job_id, stage, 30, message="Separating vocals and instrumental...")
        # Chunk inference renders a tqdm bar on stderr -> relay as 30-90%.
        sep_capture = _ProgressCapture(
            _stage_mapper(job_id, stage, 30, 90, "Separating vocals and instrumental..."),
            passthrough=sys.stderr,
        )
        with contextlib.redirect_stderr(sep_capture):
            output_files = separator.separate(str(input_path))

        del separator
        _free_gpu()

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
        _free_gpu()
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
        # Weight download renders a tqdm bar on stderr -> relay as 5-20%.
        dl_capture = _ProgressCapture(
            _stage_mapper(job_id, stage, 5, 20, f"Downloading / loading Whisper '{whisper_model}'..."),
            passthrough=sys.stderr,
        )
        # Fallback chain: OOM can strike while loading the model OR during
        # transcription itself (the pyannote VAD step allocates VRAM too, and
        # on small GPUs a big whisper model leaves it no headroom). Each
        # attempt covers the full load+transcribe cycle so any OOM degrades
        # gracefully instead of failing the job outright.
        attempts = [(DEVICE, compute_type, 8)]
        if DEVICE == "cuda":
            attempts += [("cuda", "int8_float16", 4), ("cpu", "int8", 8)]
        audio = whisperx.load_audio(vocals_path)
        result = None
        for i, (dev, ctype, batch_size) in enumerate(attempts):
            model = None
            try:
                with contextlib.redirect_stderr(dl_capture):
                    model = whisperx.load_model(
                        whisper_model,
                        dev,
                        compute_type=ctype,
                        download_root=str(MODELS_DIR / "whisper"),
                    )
                publish_progress(job_id, stage, 22, message="Transcribing vocal track...")
                # print_progress emits "Progress: N%" on stdout -> relay as 22-60%.
                tr_capture = _ProgressCapture(
                    _stage_mapper(job_id, stage, 22, 60, "Transcribing vocal track..."),
                    passthrough=sys.stdout,
                )
                with contextlib.redirect_stdout(tr_capture):
                    result = model.transcribe(audio, batch_size=batch_size, print_progress=True)
                device_used = dev
                break
            except (RuntimeError, MemoryError) as exc:
                oom = "out of memory" in str(exc).lower() or "batch_size" in str(exc)
                if not oom or i == len(attempts) - 1:
                    raise
                print(
                    f"Whisper OOM on {dev}/{ctype}; retrying with "
                    f"{attempts[i + 1][0]}/{attempts[i + 1][1]}...",
                    file=sys.stderr,
                )
                del model
                _free_gpu()
        language = result["language"]

        del model
        _free_gpu()

        publish_progress(job_id, stage, 62, message="Loading alignment model...")
        align_model, align_metadata = whisperx.load_align_model(
            language_code=language, device=device_used
        )

        publish_progress(job_id, stage, 70, message="Force-aligning words (millisecond timing)...")
        al_capture = _ProgressCapture(
            _stage_mapper(job_id, stage, 70, 95, "Force-aligning words (millisecond timing)..."),
            passthrough=sys.stdout,
        )
        with contextlib.redirect_stdout(al_capture):
            aligned = whisperx.align(
                result["segments"], align_model, align_metadata, audio, device_used,
                return_char_alignments=False,
                print_progress=True,
            )

        del align_model
        _free_gpu()

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
        _free_gpu()
        _fail(job_id, stage, exc)
        raise


# ======================================================================
# 3. Final video render (FFmpeg layering + live progress)
# ======================================================================
# xfade transitions offered for slideshow backgrounds (all built into FFmpeg).
SLIDESHOW_TRANSITIONS = [
    "fade", "dissolve", "slideleft", "slideright", "slideup", "slidedown",
    "wipeleft", "wiperight", "circleopen", "circleclose", "pixelize", "radial",
]
SLIDESHOW_TRANSITION_SECONDS = 1.0
SLIDESHOW_MAX_SLIDES = 240


def _slideshow_background(paths: list, background: dict,
                          width: int, height: int, duration: float):
    """Chain still images with xfade transitions to cover the song.

    Pure FFmpeg - each image becomes a looped clip of `slide_duration`
    seconds, consecutive clips are cross-blended with the chosen (or
    random) xfade transition. Images cycle (optionally shuffled) until
    the full duration is covered.
    """
    slide_dur = min(max(float(background.get("slide_duration", 8.0)), 3.0), 60.0)
    trans_dur = min(SLIDESHOW_TRANSITION_SECONDS, slide_dur / 3)
    choice = background.get("transition", "fade")
    shuffle = bool(background.get("shuffle"))

    n_slides = max(1, math.ceil((duration - trans_dur) / (slide_dur - trans_dur)))
    n_slides = min(n_slides, SLIDESHOW_MAX_SLIDES)

    order = []
    while len(order) < n_slides:
        batch = paths[:]
        if shuffle:
            random.shuffle(batch)
            # avoid showing the same image twice in a row across cycles
            if order and len(batch) > 1 and batch[0] == order[-1]:
                batch[0], batch[1] = batch[1], batch[0]
        order.extend(batch)
    order = order[:n_slides]

    def slide(path):
        return (
            ffmpeg.input(path, loop=1, t=slide_dur, framerate=FPS)
            .filter("scale", width, height, force_original_aspect_ratio="increase")
            .filter("crop", width, height)
            .filter("setsar", 1)
            .filter("format", "yuv420p")
        )

    # ffmpeg-python merges identical filter chains into one node, so an
    # image that cycles around needs an explicit `split` fan-out - one
    # branch per appearance in the slide order.
    counts = Counter(order)
    branches = {}
    for path, count in counts.items():
        if count == 1:
            branches[path] = [slide(path)]
        else:
            fanout = slide(path).filter_multi_output("split", count)
            branches[path] = [fanout.stream(k) for k in range(count)]
    taken = {path: 0 for path in counts}

    def next_branch(path):
        stream = branches[path][taken[path]]
        taken[path] += 1
        return stream

    video = next_branch(order[0])
    offset = slide_dur - trans_dur
    for path in order[1:]:
        trans = (
            random.choice(SLIDESHOW_TRANSITIONS) if choice == "random"
            else (choice if choice in SLIDESHOW_TRANSITIONS else "fade")
        )
        video = ffmpeg.filter(
            [video, next_branch(path)], "xfade",
            transition=trans, duration=round(trans_dur, 3), offset=round(offset, 3),
        )
        offset += slide_dur - trans_dur
    return video


def _build_ffmpeg_command(job: dict, settings: dict, work_dir: Path,
                          ass_path: Path, out_path: Path, duration: float) -> list:
    """Construct the FFmpeg arg list (no shell => no injection surface).

    Layers, bottom to top:
      background color/image/slideshow -> optional visualizer -> optional
      animated song progress bar -> optional logo watermark -> ASS subs
    Audio: the isolated instrumental stem.
    """
    width, height = RESOLUTIONS[settings["resolution"]]
    instrumental = job["instrumental_path"]

    # --- Background layer ---
    background = settings.get("background", {})
    bg_image = job.get("background_image_path")
    slideshow_paths = [
        p for p in (job.get("background_image_paths") or []) if Path(p).exists()
    ]
    if background.get("type") == "slideshow" and slideshow_paths:
        bg = _slideshow_background(slideshow_paths, background, width, height, duration)
    elif background.get("type") == "image" and bg_image and Path(bg_image).exists():
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
            # NOTE: do not insert an fps filter here - converting showfreqs'
            # native frame timing before the overlay makes FFmpeg buffer
            # frames without bound and get OOM-killed. overlay itself syncs
            # the visualizer to the 30fps main input just fine.
            vis = vis_audio.filter(
                "showfreqs", s=f"{width}x{vis_h}", mode="bar",
                fscale="log", colors=f"0x{vis_color}",
            )
        vis = vis.filter("format", "rgba").filter("colorchannelmixer", aa=opacity)
        video = ffmpeg.overlay(
            video, vis, x="(main_w-overlay_w)/2", y="(main_h-overlay_h)/2",
            eof_action="pass",
        )

    # --- Animated song progress bar (fills left to right over duration) ---
    pb_cfg = settings.get("progress_bar", {})
    if pb_cfg.get("enabled"):
        pb_color = validate_hex_color(pb_cfg.get("color", "#FFFFFF")).lstrip("#")
        pb_opacity = min(max(float(pb_cfg.get("opacity", 0.8)), 0.0), 1.0)
        pb_height_pct = min(max(float(pb_cfg.get("height", 1.5)), 0.4), 8.0)
        bar_h = max(int(height * pb_height_pct / 100), 3)
        bar = ffmpeg.input(
            f"color=c=0x{pb_color}:s={width}x{bar_h}:r={FPS}", f="lavfi"
        ).filter("format", "rgba").filter("colorchannelmixer", aa=pb_opacity)
        bar_y = 0 if pb_cfg.get("position", "bottom") == "top" else height - bar_h
        # Slide the full-width bar in from the left so the visible portion
        # tracks elapsed time: x goes from -width (0s) to 0 (end of song).
        video = ffmpeg.overlay(
            video, bar, x=f"-{width}+{width}*t/{duration:.3f}", y=bar_y,
        )

    # --- Logo / watermark overlay ---
    logo_cfg = settings.get("logo", {})
    logo_path = job.get("logo_image_path")
    if logo_cfg.get("enabled") and logo_path and Path(logo_path).exists():
        logo_size = min(max(float(logo_cfg.get("size", 0.12)), 0.03), 0.5)
        logo_opacity = min(max(float(logo_cfg.get("opacity", 1.0)), 0.0), 1.0)
        logo = (
            ffmpeg.input(logo_path)
            .filter("scale", int(width * logo_size), -1)
            .filter("format", "rgba")
            .filter("colorchannelmixer", aa=logo_opacity)
        )
        margin = int(width * 0.025)
        vert, _, horiz = logo_cfg.get("position", "top-right").partition("-")
        x_map = {
            "left": str(margin),
            "center": "(main_w-overlay_w)/2",
            "right": f"main_w-overlay_w-{margin}",
        }
        y_map = {"top": str(margin), "bottom": f"main_h-overlay_h-{margin}"}
        video = ffmpeg.overlay(
            video, logo,
            x=x_map.get(horiz, x_map["right"]),
            y=y_map.get(vert, y_map["top"]),
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
        title_text = None
        if settings.get("title_card", {}).get("enabled"):
            title_text = make_title_text(
                job.get("artist", ""), job.get("title", "")
            )
        duet_cfg = settings.get("duet", {})
        duet = None
        if duet_cfg.get("enabled"):
            duet = {
                "mode": "alternate" if duet_cfg.get("mode") == "alternate" else "markers",
                "color_b": validate_hex_color(duet_cfg.get("color_b", "#FF66CC")),
            }

        # Preview accepts the legacy bool or the full styling object.
        prev_raw = subs.get("preview")
        preview = None
        if isinstance(prev_raw, dict):
            if prev_raw.get("enabled"):
                preview = {
                    "color": validate_hex_color(prev_raw["color"]) if prev_raw.get("color") else None,
                    "scale": prev_raw.get("scale"),
                    "placement": prev_raw.get("placement"),
                }
        elif prev_raw:
            preview = {}

        font_name = subs.get("font") or "DejaVu Sans"
        if font_name not in FONT_CHOICES:
            font_name = "DejaVu Sans"

        ass_text = build_ass(
            lyrics["segments"], width, height,
            text_color=validate_hex_color(subs.get("text_color", "#FFFFFF")),
            highlight_color=validate_hex_color(subs.get("highlight_color", "#00A5FF")),
            position=subs.get("position", "bottom"),
            title_text=title_text,
            countdown=bool(subs.get("countdown")),
            preview=preview,
            duet=duet,
            font_name=font_name,
            font_scale=float(subs.get("font_scale") or 1.0),
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
        # Drain stderr on a thread so a chatty FFmpeg can't fill the pipe
        # and deadlock, and so we always have the tail for error reports.
        stderr_chunks = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True
        )
        stderr_thread.start()
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
        returncode = proc.wait()
        stderr_thread.join(timeout=5)
        stderr_tail = (stderr_chunks[0] if stderr_chunks else "")[-2000:].strip()
        if returncode != 0:
            if returncode in (-9, 137):
                stderr_tail = (
                    "process was killed (likely out of memory) - try a lower "
                    "resolution. " + stderr_tail
                ).strip()
            raise RuntimeError(
                f"FFmpeg failed with code {returncode}: {stderr_tail or 'no error output'}"
            )

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
