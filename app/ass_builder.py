"""Build karaoke-style .ass subtitle files from word-aligned lyrics.

The lyrics structure (produced by the transcription task) is:
    [{"start": float, "end": float,
      "words": [{"word": str, "start": float, "end": float}, ...]}, ...]

Karaoke behaviour uses ASS \\kf tags: text starts in SecondaryColour
(the "not yet sung" color) and sweeps to PrimaryColour (the highlight
color) exactly when each word should be sung.
"""
from .utils import hex_to_ass_color

LEAD_IN_SECONDS = 0.5   # show a line slightly before its first word
LEAD_OUT_SECONDS = 0.3  # keep a line up briefly after its last word
COUNTDOWN_SECONDS = 3.0     # length of the "get ready" dot countdown
COUNTDOWN_MIN_GAP = 3.5     # only count down after gaps at least this long


def _ass_time(seconds: float) -> str:
    """Format seconds as ASS `H:MM:SS.CC`."""
    seconds = max(0.0, seconds)
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _escape_ass_text(text: str) -> str:
    """Neutralize characters with special meaning in ASS dialogue text."""
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def make_title_text(artist: str, title: str) -> str:
    """Build the escaped two-line ASS text for the intro title card."""
    return f"{_escape_ass_text(artist)}\\N{_escape_ass_text(title)}"


def _line_text(words: list, line_start: float) -> str:
    """Build the {\\kf..}-tagged text for one dialogue line."""
    parts = []
    cursor = line_start
    for w in words:
        start = max(float(w["start"]), cursor)
        end = max(float(w["end"]), start)
        gap_cs = int(round((start - cursor) * 100))
        dur_cs = max(int(round((end - start) * 100)), 1)
        if gap_cs > 0:
            parts.append(f"{{\\kf{gap_cs}}}")  # silent wait before the word
        parts.append(f"{{\\kf{dur_cs}}}{_escape_ass_text(w['word'].strip())} ")
        cursor = end
    return "".join(parts).rstrip()


# ASS numpad alignments for centered lyric placement.
_POSITION_ALIGNMENTS = {"top": 8, "middle": 5, "bottom": 2}


def _preview_geometry(placement: str, position: str, alignment: int,
                      margin_v: int, line_offset: int,
                      width: int, height: int):
    """Resolve where the preview line lives.

    placement: "above" / "below" (relative to the main lyric line) or an
    absolute "top" / "middle" / "bottom" screen position.
    Returns (alignment, margin_v, pos_tag) - pos_tag is used where ASS
    margins can't express the location (vertical-center placements).
    """
    edge_margin = max(int(height * 0.015), 8)

    if placement in ("above", "below"):
        if position == "middle":  # margins are ignored at alignment 5
            y = height // 2 + (line_offset if placement == "below" else -line_offset)
            return 5, 0, f"{{\\pos({width // 2},{y})}}"
        # For edge positions, "away from the edge" means a larger margin.
        away = (placement == "above") if position != "top" else (placement == "below")
        margin = margin_v + line_offset if away else max(margin_v - line_offset, edge_margin)
        return alignment, margin, ""

    if placement == "middle":
        y = height // 2 + (line_offset if position == "middle" else 0)
        return 5, 0, f"{{\\pos({width // 2},{y})}}"

    # Absolute top/bottom edges; nudge away if the lyric occupies that edge.
    target_align = 8 if placement == "top" else 2
    margin = margin_v + (line_offset if placement == position else 0)
    return target_align, margin, ""


def build_ass(segments: list, width: int, height: int,
              text_color: str, highlight_color: str,
              position: str = "bottom", title_text: str = None,
              countdown: bool = False, preview: dict = None,
              duet: dict = None, font_name: str = "DejaVu Sans",
              font_scale: float = 1.0) -> str:
    """Render a complete .ass document string for the given lyric segments.

    position:   where lyric lines sit - "top", "middle", or "bottom".
    title_text: optional pre-escaped ASS text (may contain \\N) shown as an
                intro title card before the first lyric line.
    countdown:  prepend karaoke-filled dots ("● ● ●") to lines that follow
                long instrumental gaps, so singers see when to come in.
    preview:    None to disable, or {"color": "#hex"|None, "scale": float,
                "placement": "above"|"below"|"top"|"middle"|"bottom"} for
                the upcoming-line display (classic dual-line karaoke).
    duet:       optional {"mode": "markers"|"alternate", "color_b": "#hex"}.
                Singer 2 lines highlight in color_b; "markers" uses the
                per-segment "singer" field (set via "2:" prefixes in the
                lyric editor), "alternate" simply alternates lines.
    font_name:  font family for lyric text (must exist in the container).
    font_scale: multiplier on the default lyric font size (0.5 - 2.0).
    """
    font_scale = min(max(float(font_scale or 1.0), 0.5), 2.0)
    font_size = max(int(height * 0.055 * font_scale), 14)
    title_font_size = max(int(height * 0.075), 24)
    margin_v = int(height * 0.08)
    line_offset = int(font_size * 1.5)  # vertical slot for the preview line
    outline = max(int(height * 0.0035), 1)
    preview_outline = max(int(outline * 0.7), 1)
    alignment = _POSITION_ALIGNMENTS.get(position, 2)

    primary = hex_to_ass_color(highlight_color)   # sung (fill) color
    secondary = hex_to_ass_color(text_color)      # unsung color
    outline_col = "&H00000000"
    back_col = "&H80000000"

    preview = dict(preview) if preview is not None else None
    preview_pos_tag = ""
    if preview is not None:
        p_scale = min(max(float(preview.get("scale") or 0.58), 0.3), 1.0)
        preview_font_size = max(int(font_size * p_scale), 12)
        preview_col = hex_to_ass_color(
            preview.get("color") or text_color, alpha="40"
        )
        preview_align, preview_margin, preview_pos_tag = _preview_geometry(
            preview.get("placement") or "above", position, alignment,
            margin_v, line_offset, width, height,
        )
    else:  # style must still exist; it just never gets events
        preview_font_size = max(int(font_size * 0.58), 12)
        preview_col = hex_to_ass_color(text_color, alpha="40")
        preview_align, preview_margin = alignment, margin_v + line_offset

    duet_style = ""
    if duet:
        primary_b = hex_to_ass_color(duet.get("color_b", "#FF66CC"))
        duet_style = (
            f"\nStyle: KaraokeB,{font_name},{font_size},{primary_b},{secondary},"
            f"{outline_col},{back_col},-1,0,0,0,100,100,0,0,1,{outline},1,"
            f"{alignment},60,60,{margin_v},1"
        )

    header = f"""[Script Info]
Title: Off-Key Creator Karaoke
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{font_name},{font_size},{primary},{secondary},{outline_col},{back_col},-1,0,0,0,100,100,0,0,1,{outline},1,{alignment},60,60,{margin_v},1
Style: Title,{font_name},{title_font_size},{secondary},{secondary},{outline_col},{back_col},-1,0,0,0,100,100,0,0,1,{outline},1,5,60,60,{margin_v},1
Style: Preview,{font_name},{preview_font_size},{preview_col},{preview_col},{outline_col},{back_col},0,0,0,0,100,100,0,0,1,{preview_outline},1,{preview_align},60,60,{preview_margin},1{duet_style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []

    # --- Optional intro title card (artist / song name) ---
    if title_text:
        first_word_start = None
        for seg in segments:
            words = [w for w in seg.get("words", []) if w.get("word", "").strip()]
            if words:
                first_word_start = float(words[0]["start"])
                break
        card_end = 5.0 if first_word_start is None else min(5.0, first_word_start - 0.5)
        if card_end - 0.3 >= 1.5:  # only show if there's a reasonable gap
            lines.append(
                f"Dialogue: 0,{_ass_time(0.3)},{_ass_time(card_end)},Title,,0,0,0,,"
                f"{{\\fad(400,400)}}{title_text}"
            )

    # ------------------------------------------------------------------
    # Pass 1: timing plan. Each display line gets its window, singer, and
    # (optionally) a countdown extension into the preceding gap.
    # ------------------------------------------------------------------
    plan = []
    prev_end = 0.0
    visible_index = 0
    for seg in segments:
        words = [w for w in seg.get("words", []) if w.get("word", "").strip()]
        if not words:
            continue
        seg_start = float(words[0]["start"])
        seg_end = float(words[-1]["end"])
        gap = seg_start - prev_end
        has_countdown = countdown and gap >= COUNTDOWN_MIN_GAP
        if has_countdown:
            line_start = max(seg_start - COUNTDOWN_SECONDS, prev_end, 0.0)
        else:
            line_start = max(seg_start - LEAD_IN_SECONDS, prev_end, 0.0)

        singer = 1
        if duet:
            if duet.get("mode") == "alternate":
                singer = 1 + (visible_index % 2)
            else:
                singer = 2 if seg.get("singer") == 2 else 1

        plan.append({
            "words": words, "seg_start": seg_start, "seg_end": seg_end,
            "line_start": line_start, "line_end": seg_end + LEAD_OUT_SECONDS,
            "countdown": has_countdown, "singer": singer,
        })
        prev_end = seg_end
        visible_index += 1

    # Clamp each line's end so consecutive lines never stack.
    for i, entry in enumerate(plan):
        if i + 1 < len(plan):
            next_start = max(plan[i + 1]["line_start"], entry["line_start"])
            entry["line_end"] = min(entry["line_end"], max(next_start, entry["seg_end"]))
        entry["line_end"] = max(entry["line_end"], entry["seg_end"])

    # ------------------------------------------------------------------
    # Pass 2: emit events.
    # ------------------------------------------------------------------
    for i, entry in enumerate(plan):
        style = "KaraokeB" if entry["singer"] == 2 else "Karaoke"

        # Countdown dots are karaoke "words": they fill one by one with the
        # line's highlight color during the run-up, then singing starts.
        prefix = ""
        text_cursor = entry["line_start"]
        if entry["countdown"]:
            dots_span = entry["seg_start"] - entry["line_start"]
            if dots_span > 0.5:
                dot_cs = max(int(round(dots_span * 100 / 3)), 1)
                prefix = "".join(f"{{\\kf{dot_cs}}}\u25cf " for _ in range(3))
                text_cursor = entry["seg_start"]

        text = prefix + _line_text(entry["words"], text_cursor)
        lines.append(
            f"Dialogue: 0,{_ass_time(entry['line_start'])},"
            f"{_ass_time(entry['line_end'])},{style},,0,0,0,,{text}"
        )

        # Next-line preview: visible from this line's start until the next
        # line takes the main slot (covers instrumental gaps too). Layer 1
        # keeps libass from collision-shifting it against the main line.
        if preview is not None and i + 1 < len(plan):
            nxt = plan[i + 1]
            p_start, p_end = entry["line_start"], nxt["line_start"]
            if p_end - p_start >= 1.0:
                p_text = _escape_ass_text(
                    " ".join(w["word"].strip() for w in nxt["words"])
                )
                lines.append(
                    f"Dialogue: 1,{_ass_time(p_start)},{_ass_time(p_end)},"
                    f"Preview,,0,0,0,,{preview_pos_tag}{p_text}"
                )

    return header + "\n".join(lines) + "\n"
