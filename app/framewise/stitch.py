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

PIXEL_SAME = 24       # a row "didn't change" when no pixel in it differs by more than this
                      # (an average would miss a few changed letters among identical icons)
MATCH_MIN = 0.92      # how alike the strip must be where it is found
CHECK_MAX = 10.0      # mean grey difference allowed in a row of the overlap...
ROWS_AGREE = 0.8      # ...in this share of its rows (the rest may be covered by something floating)
STRIPS = 6            # strips of the new picture tried for lining it up
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
            top, bottom = _real_bands(self.prev, grey, top, bottom)
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
    same = np.abs(a - b).max(axis=1) <= PIXEL_SAME
    limit = int(h * MAX_STATIC)
    top = 0
    while top < limit and same[top]:
        top += 1
    bottom = 0
    while bottom < limit and same[h - 1 - bottom]:
        bottom += 1
    return top, bottom


def _real_bands(prev, cur, top, bottom):
    """Header and footer that really stay put. Rows can look the same in both
    pictures by chance too: a grid of identical icons scrolled by exactly one
    row. Those rows also match the page having moved, which a real header
    (or footer) doesn't, so they are counted as scrolling instead."""
    h = prev.shape[0]
    move = find_move(prev, cur, top, bottom)
    if not move:
        return top, bottom
    if top and move + top <= h and np.abs(cur[:top] - prev[move: move + top]).max() <= PIXEL_SAME:
        top = 0
    if bottom and h - bottom - move >= 0 and \
            np.abs(prev[h - bottom:] - cur[h - bottom - move: h - move]).max() <= PIXEL_SAME:
        bottom = 0
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
    if np.abs(prev[a:b] - cur[a:b]).max() <= PIXEL_SAME:
        return 0
    band = max(12, min(region // 6, 100))
    # Detailed strips in the top half of the new picture: the top is what was
    # already on screen before (lower down), when a scroll moves less than the
    # area's height. A blank strip would match anywhere. Several are tried, so
    # one that lands on something floating over the page (a chat's date
    # bubble, a "jump to bottom" button) doesn't spoil the match.
    candidates = sorted(((cur[y: y + band].std(), y)
                         for y in range(a, a + max(1, region // 2), max(4, band // 2))), reverse=True)
    candidates = [y for spread, y in candidates if spread >= 2.0][:STRIPS]
    # Strips at the very top too: only they can find a scroll of nearly the
    # whole area (the overlap is then just a little at the top).
    for y in (a, a + band // 2):
        if y not in candidates and cur[y: y + band].std() >= 2.0:
            candidates.append(y)
    if not candidates:
        return None
    area = prev[a:b].astype(np.float32)
    still = False
    best = None                           # (share of rows that agree, -mean difference, move)
    for y in candidates:
        strip = cur[y: y + band].astype(np.float32)
        scores = cv2.matchTemplate(area, strip, cv2.TM_CCOEFF_NORMED)[:, 0]
        # Every good match, not just the best: on a repeating page (a grid of
        # identical icons) the strip also matches one row off.
        peaks = [i for i in np.argsort(scores)[::-1][:40] if scores[i] >= MATCH_MIN]
        for found in peaks:
            move = (found + a) - y
            if move == 0:
                still = True              # this part didn't move (or floats over the page)
                continue
            if move < 0:
                continue
            agree, diff = _overlap(prev, cur, a, b, move)
            if best is None or (agree, -diff) > best[:2]:
                best = (agree, -diff, move)
    if best and best[0] >= ROWS_AGREE:
        return best[2]
    return 0 if still else None


def _overlap(prev, cur, a, b, move):
    """How well the pictures agree if the page moved up by `move`: the share
    of overlapping rows that match, and the mean difference. What stayed at
    the same place on the screen (a floating date bubble) is left out."""
    moved = np.abs(prev[a + move: b] - cur[a: b - move])
    floating = np.abs(prev[a: b - move] - cur[a: b - move]) <= PIXEL_SAME
    rows = np.where(floating, 0, moved).mean(axis=1)
    if not rows.size:
        return 0.0, 1e9
    return float((rows < CHECK_MAX).mean()), float(rows.mean())
