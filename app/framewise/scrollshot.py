"""Scrolling screenshot: captures a part of the screen that scrolls, all of it.

After the area is chosen (on the frozen screen, like a screenshot), the
extension turns the mouse wheel over it, a few clicks at a time; after each
scroll the area is captured again and joined to the rest (stitch.py). It
stops at the end of the page, when the pill is clicked, or at the size
limit. The first scrolls measure how far one wheel click goes, so each
scroll then moves about half the area (the pictures must overlap).
"""

import tempfile
from pathlib import Path

from gi.repository import GLib

from .stitch import Stitcher

FIRST_CLICKS = 3
TARGET = 0.5          # of the scrolling part, per scroll
SETTLE_MS = 450       # pages scroll smoothly: wait for them to stop
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
        self._tmp = Path(tempfile.mkdtemp(prefix="framewise-scroll-"))
        shell.on_scroll_stop = self.stop
        shell.show_scroll_pill(*self.rect)
        # Let the chooser's layer and the pill settle before the first picture.
        GLib.timeout_add(350, self._capture)

    def stop(self):
        self.stopped = True

    def _capture(self):
        if self.finished:
            return False
        import cv2
        path = self._tmp / "area.png"
        frame = cv2.imread(str(path)) if self.shell.screenshot_area(*self.rect, path) else None
        if frame is None:
            self._finish("The screen couldn't be captured.")
            return False
        status = self.stitcher.add(frame)
        if status == "added" and self.stitcher.last_move and self.steps:
            # How far one click moved, so the next scroll goes about half the area.
            per_click = self.stitcher.last_move / self.clicks
            region = frame.shape[0] - self.stitcher.top - self.stitcher.bottom
            self.clicks = int(max(1, min(25, round(region * TARGET / max(1.0, per_click)))))
        if status == "end":
            self._finish("" if self.steps > 1 else "This area didn't scroll.")
        elif status == "lost":
            self._finish("Stopped early: the page changed while scrolling." if self.steps > 1
                         else "This area couldn't be followed while scrolling.")
        elif status == "full":
            self._finish("Stopped at the size limit.")
        elif self.stopped or self.steps >= MAX_STEPS:
            self._finish("")
        else:
            x, y, w, h = self.rect
            self.shell.scroll_at(x + w // 2, y + h // 2, self.clicks)
            self.steps += 1
            GLib.timeout_add(SETTLE_MS, self._capture)
        return False

    def _finish(self, note):
        if self.finished:
            return
        self.finished = True
        self.shell.on_scroll_stop = None
        self.shell.hide_scroll_pill()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        self.on_done(self.stitcher.result(), note)
