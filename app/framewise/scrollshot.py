"""Scrolling screenshot: captures a part of the screen that scrolls, all of it.

After the area is chosen (on the frozen screen, like a screenshot), the
extension turns the mouse wheel over it, a few clicks at a time; after each
scroll the area is captured again and joined to the rest (stitch.py). It
stops at the end of the page, when the pill is clicked, or at the size
limit. The first scrolls measure how far one wheel click goes, so each
scroll then moves about half the area (the pictures must overlap).
Pages scroll smoothly, so after each scroll the area is captured until it
stops changing; only that still picture is used.
"""

import tempfile
from pathlib import Path

from gi.repository import GLib

from .stitch import Stitcher

FIRST_CLICKS = 3
TARGET = 0.5          # of the scrolling part, per scroll
POLL_MS = 120         # after a scroll, the area is captured this often...
MAX_POLLS = 16        # ...until it stops changing, or this many times (about 2 s)
STILL = 0.6           # mean grey difference under which two captures are the same
MAX_STEPS = 120


class ScrollCapture:
    def __init__(self, shell, rect, on_done):
        """rect: (x, y, w, h) on the desktop. on_done(picture or None, note):
        picture is an H x W x 3 BGR array."""
        self.shell = shell
        self.rect = tuple(int(round(v)) for v in rect)
        self.on_done = on_done
        self.stitcher = Stitcher()
        self.clicks = FIRST_CLICKS
        self.steps = 0
        self.stopped = False
        self.finished = False
        self._checked_end = False
        self._tmp = Path(tempfile.mkdtemp(prefix="framewise-scroll-"))
        shell.on_scroll_stop = self.stop
        shell.show_scroll_pill(*self.rect)
        # Let the chooser's layer and the pill settle before the first picture.
        GLib.timeout_add(350, self._capture)

    def stop(self):
        self.stopped = True

    def _grab(self):
        import cv2
        path = self._tmp / "area.png"
        return cv2.imread(str(path)) if self.shell.screenshot_area(*self.rect, path) else None

    def _capture(self):
        """The first picture, before any scroll."""
        if not self.finished:
            self._use(self._grab())
        return False

    def _scroll(self):
        x, y, w, h = self.rect
        self.shell.scroll_at(x + w // 2, y + h // 2, self.clicks)
        self.steps += 1
        self._before = None
        self._polls = 0
        GLib.timeout_add(POLL_MS, self._poll)

    def _poll(self):
        """Captures until the page has stopped moving, then uses that picture."""
        if self.finished:
            return False
        frame = self._grab()
        if frame is None:
            self._finish("The screen couldn't be captured.")
            return False
        import numpy as np
        settled = (self._before is not None and frame.shape == self._before.shape
                   and np.abs(frame.astype(np.int16) - self._before).mean() < STILL)
        self._polls += 1
        if settled or self._polls >= MAX_POLLS:
            self._use(frame)
            return False
        self._before = frame.astype(np.int16)
        return True

    def _use(self, frame):
        if frame is None:
            self._finish("The screen couldn't be captured.")
            return
        status = self.stitcher.add(frame)
        if status == "added" and self.stitcher.last_move and self.steps:
            # How far one click moved, so the next scroll goes about half the area
            # (growing at most twice as far each time, in case it was misjudged).
            per_click = self.stitcher.last_move / self.clicks
            region = frame.shape[0] - self.stitcher.top - self.stitcher.bottom
            want = round(region * TARGET / max(1.0, per_click))
            self.clicks = int(max(1, min(25, self.clicks * 2, want)))
        if status == "end" and self.steps and not self._checked_end:
            # Nothing moved: the end, unless the page was slow to respond. Once more.
            self._checked_end = True
            self._scroll()
            return
        if status == "added":
            self._checked_end = False
        if status == "end":
            self._finish("" if self.stitcher.parts else "This area didn't scroll.")
        elif status == "lost":
            self._finish("Stopped early: the page changed while scrolling." if self.stitcher.parts
                         else "This area couldn't be followed while scrolling.")
        elif status == "full":
            self._finish("Stopped at the size limit.")
        elif self.stopped or self.steps >= MAX_STEPS:
            self._finish("")
        else:
            self._scroll()

    def _finish(self, note):
        if self.finished:
            return
        self.finished = True
        self.shell.on_scroll_stop = None
        self.shell.hide_scroll_pill()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        self.on_done(self.stitcher.result(), note)
