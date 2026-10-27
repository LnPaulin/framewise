"""Keeps the recording controls (dot, time, pause, stop) out of the video.

The controls float over an empty part of the top bar and appear half a
second after the recording starts. Where they sit is saved in take.json
("controls"); the editor preview and the saved video cover that spot with
the same spot from the first frames, when the controls weren't there yet.
"""

import subprocess
from pathlib import Path

SHOW_DELAY_MS = 500     # the first frames show the top bar without the controls
CLEAN_AT = 0.1          # seconds into the recording the clean picture is taken from
PAD = 4                 # screen pixels around the controls that are covered too
LEFT_ROOM = 40          # ... and to their left: a longer time ("10:00") widens them that way
FILE = "controls.png"


def rect_on_monitor(rect, geometry):
    """The controls' screen rectangle (x, y, w, h) as fractions of the monitor
    (x, y, w, h), or None when they aren't on it."""
    x, y, w, h = rect
    gx, gy, gw, gh = geometry
    if w <= 0 or h <= 0 or gw <= 0 or gh <= 0:
        return None
    x0, y0 = max(gx, x - PAD - LEFT_ROOM), max(gy, y - PAD)
    x1, y1 = min(gx + gw, x + w + PAD), min(gy + gh, y + h + PAD)
    if x1 <= x0 or y1 <= y0:
        return None
    return [round((x0 - gx) / gw, 5), round((y0 - gy) / gh, 5),
            round((x1 - x0) / gw, 5), round((y1 - y0) / gh, 5)]


def info_for(rect, geometry):
    """What goes into take.json, or None."""
    fr = rect_on_monitor(rect, geometry)
    return {"rect": fr, "clean_at": CLEAN_AT} if fr else None


def pixels(take_info, size, crop_fraction=None):
    """The covered part in take pixels (even values), or None when there is
    nothing to cover or it lies outside the recorded area."""
    c = (take_info or {}).get("controls")
    if not c or not c.get("rect"):
        return None
    fx, fy, fw, fh = c["rect"]
    if crop_fraction:
        cx, cy, cw, ch = crop_fraction
        if fx >= cx + cw or fx + fw <= cx or fy >= cy + ch or fy + fh <= cy:
            return None
    vw, vh = size
    x, y = int(fx * vw) & ~1, int(fy * vh) & ~1
    x1, y1 = min(vw, int(round((fx + fw) * vw)) + 1), min(vh, int(round((fy + fh) * vh)) + 1)
    w, h = (x1 - x + 1) & ~1, (y1 - y + 1) & ~1
    w, h = min(w, (vw - x) & ~1), min(h, (vh - y) & ~1)
    return (x, y, w, h) if w >= 2 and h >= 2 else None


def in_area(take_info, crop_fraction):
    """The covered part as fractions of the recorded area (what the editor
    preview shows), or None."""
    c = (take_info or {}).get("controls")
    if not c or not c.get("rect"):
        return None
    fx, fy, fw, fh = c["rect"]
    cx, cy, cw, ch = crop_fraction or (0.0, 0.0, 1.0, 1.0)
    if fx >= cx + cw or fx + fw <= cx or fy >= cy + ch or fy + fh <= cy:
        return None
    return ((fx - cx) / cw, (fy - cy) / ch, fw / cw, fh / ch)


def patch_file(take_dir, take_info, size, crop_fraction=None):
    """The clean picture of the covered part (made once, kept in the take
    folder), or None."""
    px = pixels(take_info, size, crop_fraction)
    if px is None:
        return None
    take_dir = Path(take_dir)
    out = take_dir / FILE
    if out.exists() and out.stat().st_mtime >= (take_dir / "take.mkv").stat().st_mtime:
        return out
    x, y, w, h = px
    at = float(take_info["controls"].get("clean_at", CLEAN_AT))
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(take_dir / "take.mkv"),
                        "-ss", f"{at:.3f}", "-frames:v", "1", "-vf", f"crop={w}:{h}:{x}:{y}",
                        str(out)], capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if out.exists() and out.stat().st_size else None
