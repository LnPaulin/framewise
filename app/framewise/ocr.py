"""Reading the text in a picture, with Tesseract (the tesseract-ocr package).

Screen text is small and often light on dark, which Tesseract reads badly as
is. So the picture is enlarged first, and inverted when it is mostly dark.
Recognition runs in a background thread; the answer comes back on the main
loop.
"""

import locale
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import cairo
from gi.repository import GLib

# Locale language -> Tesseract language pack name.
LOCALE_LANGS = {
    "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "pt": "por", "nl": "nld",
    "pl": "pol", "ru": "rus", "uk": "ukr", "tr": "tur", "sv": "swe", "da": "dan",
    "nb": "nor", "fi": "fin", "cs": "ces", "el": "ell", "ar": "ara", "zh": "chi_sim",
    "ja": "jpn", "ko": "kor", "hi": "hin", "vi": "vie", "id": "ind",
}
MAX_AUTO_LANGUAGES = 4
MIN_LINE_CONFIDENCE = 40
MAX_PIXELS = 24_000_000      # after enlarging; bigger pictures are enlarged less


def available():
    return shutil.which("tesseract") is not None


def installed_languages():
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines()[1:] if line.strip() and line.strip() != "osd"]


def pick_languages(installed):
    """Every installed language at once, so nothing has to be chosen: Tesseract
    picks the best reading word by word. With many packs installed (which
    slows it down), English plus the desktop's language."""
    if len(installed) <= MAX_AUTO_LANGUAGES:
        # English first: it is the most common on screen.
        return sorted(installed, key=lambda c: c != "eng")
    langs = []
    code = (locale.getlocale()[0] or "en").split("_")[0]
    mine = LOCALE_LANGS.get(code)
    if mine in installed:
        langs.append(mine)
    if "eng" in installed:
        langs.append("eng")
    return langs or installed[:1]


def prepare(surface, out_path):
    """Writes an enlarged, dark-on-light copy of the picture for Tesseract."""
    w, h = surface.get_width(), surface.get_height()
    k = 3.0 if w * h * 9 <= MAX_PIXELS else max(1.0, min(2.0, (MAX_PIXELS / (w * h)) ** 0.5))
    tw, th = max(1, round(w * k)), max(1, round(h * k))
    out = cairo.ImageSurface(cairo.FORMAT_RGB24, tw, th)
    cr = cairo.Context(out)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.scale(tw / w, th / h)
    cr.set_source_surface(surface, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_GOOD)
    cr.paint()
    cr.identity_matrix()
    if _mostly_dark(surface):
        cr.set_operator(cairo.OPERATOR_DIFFERENCE)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
    out.write_to_png(str(out_path))


def _mostly_dark(surface):
    small = cairo.ImageSurface(cairo.FORMAT_RGB24, 48, 48)
    cr = cairo.Context(small)
    cr.scale(48 / surface.get_width(), 48 / surface.get_height())
    cr.set_source_surface(surface, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_GOOD)
    cr.paint()
    small.flush()
    data = small.get_data()
    total = 0
    for i in range(0, len(data), 4):      # B G R x
        total += 0.11 * data[i] + 0.59 * data[i + 1] + 0.3 * data[i + 2]
    return total / (len(data) // 4) < 110


# Bullets Tesseract's English model reads as letters or signs.
BULLET_MISREADS = re.compile(r"^(\s*)(?:[@©®«»*°¢]|e(?= [A-Z]))\s+")


def tidy(text):
    text = text.replace("\f", "")
    lines = [BULLET_MISREADS.sub(r"\1• ", line.rstrip()) for line in text.splitlines()]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def _tesseract(path, langs, psm):
    cmd = ["tesseract", str(path), "-", "--psm", str(psm), "-c", "preserve_interword_spaces=1"]
    if langs:
        cmd += ["-l", "+".join(langs)]
    done = subprocess.run(cmd + ["tsv"], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "tesseract failed")
    return layout(done.stdout)


def layout(tsv):
    """Text from Tesseract's word boxes: blocks apart by a blank line, and
    the indentation of each line kept (so code stays readable)."""
    blocks = {}
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        block = int(f[2])
        line = (int(f[3]), int(f[4]))
        left, width = int(f[6]), int(f[8])
        blocks.setdefault(block, {}).setdefault(line, []).append((left, width, f[11], float(f[10])))
    parts = []
    for block in sorted(blocks):
        # Text cut in half at the edge of the area reads as low-confidence junk.
        lines = [sorted(w[:3] for w in words) for _key, words in sorted(blocks[block].items())
                 if sum(w[3] for w in words) / len(words) >= MIN_LINE_CONFIDENCE]
        if not lines:
            continue
        chars = sum(len(t) for ws in lines for _l, _w, t in ws)
        char_w = max(1.0, sum(w for ws in lines for _l, w, _t in ws) / max(1, chars))
        margin = min(ws[0][0] for ws in lines)
        out = []
        for ws in lines:
            indent = round((ws[0][0] - margin) / char_w)
            text = ws[0][2]
            for (pl, pw, _pt), (l, _w, t) in zip(ws, ws[1:]):
                gap = round((l - pl - pw) / char_w)
                text += " " * max(1, gap if gap >= 3 else 1) + t
            out.append(" " * indent + text)
        parts.append("\n".join(out))
    return tidy("\n\n".join(parts))


def read_text(surface):
    """The text in a cairo picture (blocking). Raises RuntimeError if it can't run."""
    if not available():
        raise RuntimeError("Text recognition needs the tesseract-ocr package")
    langs = pick_languages(installed_languages())
    with tempfile.TemporaryDirectory(prefix="framewise-ocr-") as tmp:
        path = Path(tmp) / "in.png"
        prepare(surface, path)
        text = _tesseract(path, langs, 3)          # whole page, finds columns
        if not text:
            text = _tesseract(path, langs, 6)      # one block: better for a word or a line
    return text


def read_text_async(surface, on_done):
    """on_done(text, error) on the main loop."""
    def work():
        try:
            text, error = read_text(surface), None
        except (RuntimeError, OSError, subprocess.SubprocessError, cairo.Error) as e:
            text, error = "", str(e)
        GLib.idle_add(lambda: on_done(text, error) and False)
    threading.Thread(target=work, daemon=True).start()


def preview(text, limit=140):
    """The copied text on one line, for a notification."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def copy_text(text):
    try:
        subprocess.run(["wl-copy"], input=text.encode(), timeout=10, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
