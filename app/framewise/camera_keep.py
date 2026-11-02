"""Keeps the camera bubble where it was when the video zooms.

The bubble is a window on the screen, so it is recorded with everything
else, and a zoom (on clicks, on typing, or a set spot) would enlarge it or
push it out of the picture. While recording, where the bubble's picture is
on screen is noted (take.json "camera"); the editor preview and the saved
video then draw that part of the recording again over the zoomed picture,
unzoomed, in the same place, so the camera stays put whatever the zoom does.
Under it, the bubble (with its shadow) is first filled in from the colours
around it, so the zoomed picture doesn't show a second, enlarged camera.
"""

import math

SHADOW = (18, 14, 18, 32)   # screen pixels the bubble's shadow reaches: left, top, right, bottom
POLL_MS = 500          # how often the bubble's place is checked while recording
MAX_SPOTS = 40         # a bubble moved around a lot: only the first places are kept


def screen_box(window_rect, picture):
    """The camera picture on screen: ((x, y, w, h), radii), from the bubble
    window's place and bubble.picture_box(); or None."""
    if not window_rect or not picture:
        return None
    (px, py, pw, ph), radii = picture
    x, y = window_rect[0] + px, window_rect[1] + py
    return (round(x), round(y), round(pw), round(ph)), tuple(round(r, 1) for r in radii)


class Track:
    """Where the bubble's picture was while recording, on the monotonic clock.

    A place counts once it is seen twice in a row: while the bubble is being
    dragged, it moves too fast to follow and is left alone."""

    def __init__(self):
        self.spots = []        # [start, end or None, (x, y, w, h), radii]
        self._last = None
        self._still = None     # the latest time the current place was seen

    def add(self, mono, box):
        seen, self._last = self._last, box
        current = self.spots[-1] if self.spots and self.spots[-1][1] is None else None
        if current and box == (current[2], current[3]):
            self._still = mono
            return
        if current:
            current[1] = self._still              # it moved or went away after this
        if box and box == seen and len(self.spots) < MAX_SPOTS:
            self.spots.append([mono, None, box[0], box[1]])
            self._still = mono

    def info(self, runs, geometry, end_mono):
        """What goes into take.json: spots on the recording's clock, as
        fractions of the monitor (radii as fractions of the picture width)."""
        gx, gy, gw, gh = geometry
        out = []
        for start, end, (x, y, w, h), radii in self.spots:
            a = _recording_time(runs, start, forward=True)
            b = _recording_time(runs, end if end is not None else end_mono, forward=False)
            if a is None or b is None or b - a < 0.05 or w <= 0:
                continue
            sl, st, sr, sb = SHADOW
            out.append({"start": round(a, 3), "end": round(b, 3),
                        "rect": [round((x - gx) / gw, 5), round((y - gy) / gh, 5),
                                 round(w / gw, 5), round(h / gh, 5)],
                        "radii": [round(r / w, 4) for r in radii],
                        "shadow": [round(sl / gw, 5), round(st / gh, 5), round(sr / gw, 5), round(sb / gh, 5)]})
        return out or None


def _recording_time(runs, mono, forward):
    """Seconds into the recording; a time while paused moves to the next run
    (forward) or the end of the previous one."""
    best = None
    for start, end, offset in runs:
        stop = end if end is not None else math.inf
        if start <= mono < stop:
            return offset + (mono - start)
        if forward and mono < start:
            return offset
        if not forward and mono >= stop:
            best = offset + (stop - start)
    return best


def in_area(take_info, crop_fraction=None):
    """The spots as fractions of the recorded area (what the editor shows):
    [(start, end, (x, y, w, h), radii as fractions of the width, outer)],
    outer being the picture with its shadow (x, y, w, h): the part filled in."""
    cx, cy, cw, ch = crop_fraction or (0.0, 0.0, 1.0, 1.0)
    out = []
    for s in (take_info or {}).get("camera") or ():
        fx, fy, fw, fh = s["rect"]
        x, y, w, h = (fx - cx) / cw, (fy - cy) / ch, fw / cw, fh / ch
        if x >= 1 or y >= 1 or x + w <= 0 or y + h <= 0:
            continue                       # not in the recorded area
        # Older takes didn't note the shadow: about that much of a usual bubble.
        sl, st, sr, sb = s.get("shadow") or (0.07 * fw, 0.055 * fh, 0.07 * fw, 0.12 * fh)
        ox, oy = max(0.0, x - sl / cw), max(0.0, y - st / ch)
        outer = (ox, oy, min(1.0, x + w + sr / cw) - ox, min(1.0, y + h + sb / ch) - oy)
        out.append((s["start"], s["end"], (x, y, w, h), tuple(s.get("radii") or (0, 0, 0, 0)), outer))
    return out


def fill_sigma(w, h):
    """How strongly the filled-in spot is blurred (pixels of the take)."""
    return max(2.0, min(w, h) / 5)


def mask(w, h, radii_px):
    """A white-on-black picture of the camera's shape (h x w uint8), softened
    by one pixel at the edge."""
    import numpy as np
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32) + 0.5
    inside = np.ones((h, w), np.float32)
    tl, tr, br, bl = radii_px
    for r, cx, cy, sx, sy in ((tl, tl, tl, -1, -1), (tr, w - tr, tr, 1, -1),
                              (br, w - br, h - br, 1, 1), (bl, bl, h - bl, -1, 1)):
        if r <= 0.5:
            continue
        corner = ((xx - cx) * sx > 0) & ((yy - cy) * sy > 0)
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        inside = np.where(corner, np.clip(r - d + 0.5, 0, 1), inside)
    return (inside * 255).astype(np.uint8)


def export_spots(take_info, crop_fraction, base, out_rect, area, work_dir):
    """The spots for the saved video: where to cut each one from the take
    (even take pixels) and where it goes on the picture after the zoom
    (base -> out_rect is the picture's unzoomed scale), with a mask file."""
    import cv2
    from pathlib import Path
    ax, ay, aw, ah = area
    bx, by, bw, bh = base
    _rx, _ry, rw, rh = out_rect
    kx, ky = rw / bw, rh / bh
    spots = []
    for k, (start, end, (x, y, w, h), radii, outer) in enumerate(in_area(take_info, crop_fraction)):
        # In take pixels, then only the part inside the shown picture.
        x0, y0 = ax + x * aw, ay + y * ah
        x1, y1 = x0 + w * aw, y0 + h * ah
        cx0, cy0 = max(bx, int(math.floor(x0)) & ~1), max(by, int(math.floor(y0)) & ~1)
        cx1, cy1 = min(bx + bw, int(math.ceil(x1))), min(by + bh, int(math.ceil(y1)))
        cw, ch = (cx1 - cx0) & ~1, (cy1 - cy0) & ~1
        if cw < 4 or ch < 4:
            continue
        ow, oh = max(2, int(round(cw * kx)) & ~1), max(2, int(round(ch * ky)) & ~1)
        ox, oy = int(round((cx0 - bx) * kx)), int(round((cy0 - by) * ky))
        # The whole shape at the output size, cut to the part that shows.
        full_w, full_h = (x1 - x0) * kx, (y1 - y0) * ky
        shape = mask(max(2, int(round(full_w))), max(2, int(round(full_h))),
                     [r * full_w for r in radii])
        sx, sy = int(round((cx0 - x0) * kx)), int(round((cy0 - y0) * ky))
        part = shape[max(0, sy): max(0, sy) + oh, max(0, sx): max(0, sx) + ow]
        if part.shape != (oh, ow):
            part = cv2.copyMakeBorder(part, 0, oh - part.shape[0], 0, ow - part.shape[1],
                                      cv2.BORDER_REPLICATE)
        path = Path(work_dir) / f"camera_mask_{k}.png"
        cv2.imwrite(str(path), part)
        # The part filled in under it (ffmpeg's delogo needs a pixel of border).
        fx0, fy0 = max(1, int(ax + outer[0] * aw)), max(1, int(ay + outer[1] * ah))
        fx1 = min(ax + aw - 1, int(math.ceil(ax + (outer[0] + outer[2]) * aw)))
        fy1 = min(ay + ah - 1, int(math.ceil(ay + (outer[1] + outer[3]) * ah)))
        fill = (fx0, fy0, fx1 - fx0, fy1 - fy0) if fx1 - fx0 >= 4 and fy1 - fy0 >= 4 else None
        spots.append({"start": start, "end": end, "crop": (cx0, cy0, cw, ch),
                      "at": (ox, oy), "size": (ow, oh), "mask": str(path), "fill": fill,
                      "sigma": fill_sigma(fill[2], fill[3]) if fill else 0})
    return spots
