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


def build_ass(segments: list, width: int, height: int,
              text_color: str, highlight_color: str) -> str:
    """Render a complete .ass document string for the given lyric segments."""
    font_size = max(int(height * 0.055), 18)
    margin_v = int(height * 0.08)
    outline = max(int(height * 0.0035), 1)

    primary = hex_to_ass_color(highlight_color)   # sung (fill) color
    secondary = hex_to_ass_color(text_color)      # unsung color
    outline_col = "&H00000000"
    back_col = "&H80000000"

    header = f"""[Script Info]
Title: Off-Key Creator Karaoke
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,DejaVu Sans,{font_size},{primary},{secondary},{outline_col},{back_col},-1,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []
    prev_end = 0.0
    for i, seg in enumerate(segments):
        words = [w for w in seg.get("words", []) if w.get("word", "").strip()]
        if not words:
            continue
        seg_start = float(words[0]["start"])
        seg_end = float(words[-1]["end"])
        line_start = max(seg_start - LEAD_IN_SECONDS, prev_end, 0.0)
        line_end = seg_end + LEAD_OUT_SECONDS
        # Avoid stacking: clamp this line's end to the next line's start.
        if i + 1 < len(segments):
            nxt = [w for w in segments[i + 1].get("words", []) if w.get("word", "").strip()]
            if nxt:
                next_start = max(float(nxt[0]["start"]) - LEAD_IN_SECONDS, line_start)
                line_end = min(line_end, max(next_start, seg_end))
        line_end = max(line_end, seg_end)
        prev_end = line_end
        text = _line_text(words, line_start)
        lines.append(
            f"Dialogue: 0,{_ass_time(line_start)},{_ass_time(line_end)},Karaoke,,0,0,0,,{text}"
        )

    return header + "\n".join(lines) + "\n"
