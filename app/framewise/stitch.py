"""Joins the pictures of a scrolling screenshot into one tall picture.

Each scroll must move less than about 60% of the scrolling part, so the
pictures overlap (scrollshot.py aims for half).

After each scroll the same area is captured again. Each new picture is
lined up with the previous one by finding where a detailed strip of it sat
before (OpenCV template matching); the rows that scrolled into view are
added below. Parts that stay put while the rest scrolls (a site's header
at the top, a chat's typing box at the bottom) are found on the first
scroll and kept only once: the header at the top, the footer at the end.
"""

import numpy as np

ROW_SAME = 2.0        # mean grey difference under which a row "didn't change"
MATCH_MIN = 0.92      # how alike the strip must be where it is found
CHECK_MAX = 10.0      # mean grey difference allowed over the whole overlap
MAX_STATIC = 0.4      # header and footer each take at most this share of the height
MAX_HEIGHT = 30000    # pixels; longer pages stop here


class Stitcher:
    def __init__(self):
        self.parts = []          # colour rows, top to bottom (without the footer)
        self.prev = None         # previous picture (grey, int16)
        self.last = None         # previous picture (colour)
        self.top = self.bottom = None   # header and footer heights, found on the first scroll
        self.height = 0
        self.last_move = 0

    def add(self, frame):
        """frame: H x W x 3 uint8 (BGR or RGB, the same each time).
        Returns "added", "end" (nothing moved: the bottom was reached),
        "lost" (it couldn't be lined up) or "full"."""
        grey = _grey(frame)
        if self.prev is None:
            self.prev, self.last = grey, frame
            self.height = frame.shape[0]
            return "added"
        if grey.shape != self.prev.shape:
            return "lost"
        h = grey.shape[0]
        if self.top is None:
            top, bottom = static_bands(self.prev, grey)
            if top + bottom >= h - 8:
                return "end"                      # nothing scrolled
            self.top, self.bottom = top, bottom
            self.parts.append(self.last[: h - bottom])
        move = find_move(self.prev, grey, self.top, self.bottom)
        if move is None:
            return "lost"
        if move == 0:
            return "end"
        b = h - self.bottom
        self.parts.append(frame[b - move: b])
        self.height += move
        self.last_move = move
        self.prev, self.last = grey, frame
        return "full" if self.height >= MAX_HEIGHT else "added"

    def result(self):
        """The joined picture (H x W x 3 uint8)."""
        if self.last is None:
            return None
        if not self.parts:
            return self.last
        footer = self.last[self.last.shape[0] - self.bottom:] if self.bottom else self.last[:0]
        return np.ascontiguousarray(np.vstack(self.parts + [footer]))


def _grey(frame):
    f = frame.astype(np.int16)
    return (f[..., 0] * 3 + f[..., 1] * 6 + f[..., 2]) // 10 if f.ndim == 3 else f


def static_bands(a, b):
    """Heights of the rows at the top and at the bottom that are the same in
    both pictures (header and footer), each at most MAX_STATIC of the height."""
    h = a.shape[0]
    same = np.abs(a - b).mean(axis=1) < ROW_SAME
    limit = int(h * MAX_STATIC)
    top = 0
    while top < limit and same[top]:
        top += 1
    bottom = 0
    while bottom < limit and same[h - 1 - bottom]:
        bottom += 1
    return top, bottom


def find_move(prev, cur, top, bottom):
    """How many pixels the content moved up between prev and cur (0: not at
    all), or None when it can't be told."""
    import cv2
    h = prev.shape[0]
    a, b = top, h - bottom
    region = b - a
    if region < 24:
        return None
    if np.abs(prev[a:b] - cur[a:b]).mean() < ROW_SAME:
        return 0
    band = max(12, min(region // 6, 100))
    # The most detailed strip near the top of the new picture: the top is
    # what was already on screen before (lower down), when a scroll moves
    # less than the area's height. A blank strip would match anywhere.
    best, start = -1.0, a
    for y in range(a, a + max(1, region // 8), max(4, band // 4)):
        spread = cur[y: y + band].std()
        if spread > best:
            best, start = spread, y
    if best < 2.0:
        return None
    strip = cur[start: start + band].astype(np.float32)
    area = prev[a:b].astype(np.float32)
    scores = cv2.matchTemplate(area, strip, cv2.TM_CCOEFF_NORMED)
    _min, score, _minloc, (_x, found) = cv2.minMaxLoc(scores)
    if score < MATCH_MIN:
        return None
    move = (found + a) - start
    if move < 0:
        return None
    if move == 0:
        return 0
    # The whole overlap must agree, not just the strip.
    overlap = np.abs(prev[a + move: b].astype(np.float32) - cur[a: b - move]).mean()
    return move if overlap < CHECK_MAX else None
