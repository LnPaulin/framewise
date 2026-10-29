"""Captions: the words spoken in a recording, as short lines on the video.

The speech model's words (with their times) are grouped into lines that are
easy to read: at most MAX_CHARS characters, broken at the end of a sentence,
at a pause, or when the line is full. Filler sounds (um, uh) are left out.
Times are recording time, like every other edit; the saved video draws them
with ffmpeg's subtitle renderer (an .ass file), and "Save Captions" writes
an .srt in the finished video's time.
"""

from . import edit_model as em
from . import speech

MAX_CHARS = 42        # per line (a common subtitle limit)
PAUSE = 0.7           # a silence this long starts a new line
SENTENCE_MIN = 18     # a sentence end breaks the line once it has this many characters
MIN_SHOW = 0.9        # seconds a line stays at least
HOLD = 0.4            # seconds a line stays after its last word (when nothing follows)
FONT = "Ubuntu"


def build(words):
    """[{word, start, end}] -> caption items [{id, start, end, text}]."""
    lines, current = [], []

    def close():
        if current:
            lines.append(current[:])
            current.clear()

    for w in words or []:
        text = str(w.get("word", "")).strip()
        if not text or speech.is_filler(text):
            continue
        if current:
            gap = w["start"] - current[-1]["end"]
            length = len(" ".join(x["word"] for x in current)) + 1 + len(text)
            if gap >= PAUSE or length > MAX_CHARS:
                close()
        current.append({"word": text, "start": float(w["start"]), "end": float(w["end"])})
        joined = " ".join(x["word"] for x in current)
        if text[-1:] in ".?!" and len(joined) >= SENTENCE_MIN:
            close()
    close()

    items = []
    for i, line in enumerate(lines):
        start, end = line[0]["start"], line[-1]["end"]
        nxt = lines[i + 1][0]["start"] if i + 1 < len(lines) else None
        end = max(end + HOLD, start + MIN_SHOW)
        if nxt is not None:
            end = min(end, nxt - 0.05)
        end = max(end, start + 0.2)
        text = " ".join(x["word"] for x in line)
        # A capital only where a sentence starts (not on the rest of a long one).
        starts_sentence = i == 0 or lines[i - 1][-1]["word"][-1:] in ".?!"
        if starts_sentence:
            text = text[:1].upper() + text[1:]
        items.append({"id": em.new_id(), "start": round(start, 3), "end": round(end, 3), "text": text})
    return items


def at(doc, t):
    """The caption shown at recording time t, or None."""
    cap = doc.get("captions") or {}
    if not cap.get("on"):
        return None
    for c in cap.get("items") or []:
        if c["start"] <= t < c["end"]:
            return c
        if c["start"] > t:
            break
    return None


def font_px(doc, canvas_h):
    return max(12, round(canvas_h * em.CAPTION_SIZES[doc["captions"]["size"]]))


# ---- files -----------------------------------------------------------------------------

def _srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(doc):
    """The captions as SubRip text, timed for the finished video (cuts and
    speed-ups applied; lines in cut parts left out)."""
    speed = em.normalize_export(doc.get("export"))["speed"]
    out, n = [], 0
    for c in (doc.get("captions") or {}).get("items") or []:
        a = em.to_finished(doc, c["start"])
        b = em.to_finished(doc, c["end"])
        if a is None and b is None:
            # Starts and ends in removed parts: keep it only if something in between is kept.
            mid = em.to_finished(doc, (c["start"] + c["end"]) / 2)
            if mid is None:
                continue
            a = b = mid
        if a is None:
            a = _first_kept_after(doc, c["start"], c["end"])
        if b is None:
            b = _last_kept_before(doc, c["start"], c["end"])
        if a is None or b is None or b - a < 0.05:
            continue
        n += 1
        out.append(f"{n}\n{_srt_time(a / speed)} --> {_srt_time(b / speed)}\n{c['text']}\n")
    return "\n".join(out)


def _first_kept_after(doc, a, b):
    for x, y, _r in em.pieces(doc):
        if y > a and x < b:
            return em.to_finished(doc, max(a, x))
    return None


def _last_kept_before(doc, a, b):
    best = None
    for x, y, _r in em.pieces(doc):
        if y > a and x < b:
            best = em.to_finished(doc, min(b, y) - 1e-4)
    return best


def _ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text):
    # Braces start override codes in ASS, a backslash starts escapes.
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def to_ass(doc, width, height):
    """The captions for the saved video, in recording time, drawn on a
    width x height picture: white text on a soft dark box."""
    cap = doc["captions"]
    size = font_px(doc, height)
    align = 2 if cap["position"] == "bottom" else 8          # bottom / top centre
    margin_v = round(height * 0.06)
    margin_h = round(width * 0.08)
    outline = max(2, round(size * 0.32))                       # the box's padding
    head = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{FONT},{size},&H00FFFFFF,&H00FFFFFF,&H40141418,&H40141418,"
        f"-1,0,0,0,100,100,0,0,3,{outline},0,{align},{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = [f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},Caption,,0,0,0,,{_ass_text(c['text'])}"
             for c in cap["items"]]
    return head + "\n".join(lines) + "\n"
