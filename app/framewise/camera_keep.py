"""Keeps the camera bubble where it was when the video zooms.

The bubble is a window on the screen, so it is recorded with everything
else (GNOME can't leave one window out of a screen recording). While
recording, where the bubble's picture is on screen is noted (take.json
"camera"). The editor preview and the saved video then:

- draw that part of the recording on top of the zoomed picture, unzoomed and
  in its shape, so the camera stays in its corner whatever the zoom does;
- steer each zoom clear of the bubble's spot, so the zoomed picture never
  shows the camera a second time, enlarged.
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
    outer being the picture with its shadow (x, y, w, h): what zooms keep clear of."""
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
        spots.append({"start": start, "end": end, "crop": (cx0, cy0, cw, ch),
                      "at": (ox, oy), "size": (ow, oh), "mask": str(path)})
    return spots


SMOOTH_S = 0.15     # seconds over which a zoom's sideways step is eased


def steer(views, spots, source, fps):
    """Moves each zoomed view (x, y, w, h, fractions of the shown part) off
    the bubble's spot: left, up, right or down, whichever is the shortest
    step that stays inside the picture. If no step clears it (a click on the
    bubble itself), the one that leaves the least of it in view. The steps
    are eased over time so the view glides instead of jumping."""
    if not views or not spots:
        return views
    sx, sy, sw, sh = source
    boxes = [(a, b, ((ox - sx) / sw, (oy - sy) / sh, ow / sw, oh / sh))
             for a, b, _r, _rad, (ox, oy, ow, oh) in spots]
    dx, dy = [0.0] * len(views), [0.0] * len(views)
    for i, (x, y, w, h) in enumerate(views):
        if w >= 0.999 and h >= 0.999:
            continue                       # not zoomed: the camera layer covers its own spot
        t = i / fps
        for a, b, box in boxes:
            if a <= t <= b:
                dx[i], dy[i] = _step((x, y, w, h), box)
                break
    dx, dy = _ease(dx, fps), _ease(dy, fps)
    out = []
    for (x, y, w, h), ex, ey in zip(views, dx, dy):
        out.append((min(max(0.0, x + ex), 1 - w), min(max(0.0, y + ey), 1 - h), w, h))
    return out


def _overlap(v, box):
    x, y, w, h = v
    bx, by, bw, bh = box
    return max(0.0, min(x + w, bx + bw) - max(x, bx)) * max(0.0, min(y + h, by + bh) - max(y, by))


def _step(v, box):
    x, y, w, h = v
    bx, by, bw, bh = box
    if _overlap(v, box) <= 0:
        return 0.0, 0.0
    best = None
    for ex, ey in ((bx - (x + w), 0.0), (0.0, by - (y + h)), (bx + bw - x, 0.0), (0.0, by + bh - y)):
        # As far as the picture allows in that direction.
        ex = min(max(ex, -x), 1 - w - x)
        ey = min(max(ey, -y), 1 - h - y)
        left = _overlap((x + ex, y + ey, w, h), box)
        key = (round(left, 6), abs(ex) + abs(ey))
        if best is None or key < best[0]:
            best = (key, ex, ey)
    return best[1], best[2]


def _ease(values, fps):
    """Smooths steps both ways in time (no lag), keeping the larger step where
    one is needed: the view is never eased back onto the bubble."""
    k = 1 - math.exp(-1 / max(1.0, SMOOTH_S * fps))
    fwd, cur = [], 0.0
    for v in values:
        cur += (v - cur) * k
        fwd.append(cur)
    back, cur = [0.0] * len(values), 0.0
    for i in range(len(values) - 1, -1, -1):
        cur += (values[i] - cur) * k
        back[i] = cur
    out = []
    for v, f, b in zip(values, fwd, back):
        e = (f + b) / 2
        # Keep at least the step needed, in its direction.
        out.append(min(e, v) if v < 0 else max(e, v) if v > 0 else e)
    return out
