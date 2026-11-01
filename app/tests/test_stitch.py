import unittest
from unittest import mock

import numpy as np

from framewise import stitch

try:
    import cv2  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


def page(height=5000, width=500, seed=1):
    """A long, varied 'web page': lines of text-like noise and some blank gaps."""
    rng = np.random.default_rng(seed)
    p = np.full((height, width, 3), 245, np.uint8)
    y = 10
    while y < height - 30:
        if rng.random() < 0.15:
            y += int(rng.integers(30, 80))            # a blank gap
            continue
        n = int(rng.integers(40, width - 40))
        line = rng.integers(0, 120, (14, n, 3), dtype=np.uint8)
        p[y: y + 14, 20: 20 + n] = line
        y += 22
    return p


def viewport(p, offset, view=600, header=0, footer=0):
    """What the screen shows when scrolled to offset, with a fixed header/footer."""
    frame = p[offset: offset + view].copy()
    if header:
        frame[:header] = (60, 90, 200)
        frame[header // 2, 10:200] = 255                   # a logo
    if footer:
        frame[view - footer:] = (40, 40, 40)
        frame[view - footer // 2, 10:300] = 200
    return frame


@unittest.skipUnless(HAVE, "OpenCV not installed")
class StitchTests(unittest.TestCase):
    def capture(self, p, steps, view=600, header=0, footer=0):
        s = stitch.Stitcher()
        results = []
        offset = 0
        for step in steps + [0, 0]:
            results.append(s.add(viewport(p, offset, view, header, footer)))
            if results[-1] == "end":
                break
            offset = min(offset + step, len(p) - view)
        return s, results

    def test_plain_page(self):
        p = page(3000)
        s, results = self.capture(p, [350] * 10)
        self.assertEqual(results[-1], "end")
        self.assertNotIn("lost", results)
        out = s.result()
        self.assertEqual(out.shape, p.shape)
        self.assertTrue(np.array_equal(out, p))

    def test_uneven_steps(self):
        p = page(2600, seed=3)
        s, results = self.capture(p, [120, 333, 287, 301, 59, 340, 310, 222, 350])
        self.assertNotIn("lost", results)
        out = s.result()
        self.assertEqual(out.shape[0], 2600)
        self.assertTrue(np.array_equal(out, p))

    def test_fixed_header_and_footer_once(self):
        p = page(3000, seed=5)
        header, footer = 64, 50
        s, results = self.capture(p, [243] * 14, header=header, footer=footer)   # half of the scrolling part
        self.assertNotIn("lost", results)
        out = s.result()
        # Header + every page row under it + footer, each once.
        self.assertEqual(out.shape[0], header + (3000 - header - footer) + footer)
        self.assertTrue(np.array_equal(out[:header], viewport(p, 0, header=header)[:header]))
        self.assertTrue(np.array_equal(out[-footer:], viewport(p, 0, footer=footer)[-footer:]))
        self.assertTrue(np.array_equal(out[header:-footer], p[header: 3000 - footer]))

    def test_chat_with_floating_date_and_button(self):
        # R.20: a chat shows a date bubble at the top and a "jump to bottom"
        # button while it scrolls; they float over the messages.
        p = page(height=3000, seed=4)
        s, offset, statuses = stitch.Stitcher(), 0, []
        for i, step in enumerate([200, 260, 240, 300, 250, 280, 0, 0]):
            frame = viewport(p, offset)
            if i:                                            # appear once scrolling starts
                frame[8:36, 200:300] = (90, 120, 160)        # the date bubble
                frame[8 + 14, 215:285] = 255
                if i % 2:
                    frame[540:584, 440:484] = (50, 50, 50)   # the round button, on and off
            statuses.append(s.add(frame))
            if statuses[-1] == "end":
                break
            offset = min(offset + step, len(p) - 600)
        self.assertNotIn("lost", statuses)
        result = s.result()
        # Apart from where the floating things sat, the picture is the page.
        self.assertEqual(result.shape[0], offset + 600)
        self.assertLess(np.abs(result[:, :180].astype(int) - p[: result.shape[0], :180]).mean(), 1.0)

    def test_grid_of_identical_icons(self):
        # Files: the same icon in every cell, only the names differ; a scroll of
        # exactly one row must still count as moving.
        rng = np.random.default_rng(9)
        p = np.full((2700, 600, 3), 250, np.uint8)
        for r in range(30):
            for c in range(6):
                y, x = r * 90, c * 100
                p[y + 10: y + 60, x + 30: x + 70] = 210            # the icon
                p[y + 65: y + 78, x + 20: x + 20 + int(rng.integers(40, 70))] = rng.integers(0, 120, (13, 1, 3))
        s, statuses = self.capture(p, [90, 180, 90, 270, 90])
        self.assertNotIn("lost", statuses)
        self.assertEqual(statuses.count("added"), 6)
        self.assertEqual((s.top, s.bottom), (0, 0))       # no fixed header: it's all icons
        self.assertTrue(np.array_equal(s.result(), p[: s.result().shape[0]]))

    def test_grid_starting_mid_row(self):
        # The same, with the view starting part-way into a row (as in Files).
        rng = np.random.default_rng(3)
        p = np.full((2730, 600, 3), 250, np.uint8)
        for r in range(30):
            for c in range(6):
                y, x = 30 + r * 90, c * 100
                p[y + 10: y + 60, x + 30: x + 70] = 210
                p[y + 65: y + 78, x + 20: x + 20 + int(rng.integers(40, 70))] = rng.integers(0, 120, (13, 1, 3))
        s, statuses = self.capture(p[20:], [90, 270, 180], view=495)
        self.assertNotIn("lost", statuses)
        self.assertTrue(np.array_equal(s.result(), p[20: 20 + s.result().shape[0]]))

    def test_nothing_scrolls(self):
        p = page(600)
        s = stitch.Stitcher()
        self.assertEqual(s.add(viewport(p, 0)), "added")
        self.assertEqual(s.add(viewport(p, 0)), "end")
        self.assertTrue(np.array_equal(s.result(), viewport(p, 0)))

    def test_unrelated_picture_is_lost(self):
        s = stitch.Stitcher()
        s.add(viewport(page(2000, seed=1), 0))
        self.assertEqual(s.add(viewport(page(2000, seed=9), 0)), "lost")

    def test_blank_page_is_lost_not_wrong(self):
        s = stitch.Stitcher()
        blank = np.full((600, 500, 3), 250, np.uint8)
        moved = blank.copy()
        moved[590:, :] = 0
        s.add(blank)
        self.assertIn(s.add(moved), ("lost", "end"))


if __name__ == "__main__":
    unittest.main()


class SmoothShell:
    """A pretend screen showing a page that scrolls smoothly, the way a
    browser does: a scroll keeps moving the page for a few captures."""

    def __init__(self, p, view=600, per_click=40, frames=5):
        self.p, self.view, self.per_click, self.frames = p, view, per_click, frames
        self.offset, self.target = 0, 0
        self.on_scroll_stop = None
        self.max_offset = p.shape[0] - view

    def show_scroll_pill(self, *a):
        pass

    def hide_scroll_pill(self):
        pass

    def scroll_at(self, x, y, clicks, smooth=False):
        self.target = min(self.max_offset, self.target + clicks * self.per_click)

    def screenshot_area(self, x, y, w, h, path):
        import cv2
        if self.offset < self.target:      # still gliding
            self.offset = min(self.target, self.offset + max(1, (self.target - self.offset) // 2 + 3))
        cv2.imwrite(str(path), viewport(self.p, self.offset, self.view))
        return True


class ScrollCaptureTests(unittest.TestCase):
    def run_capture(self, shell):
        from gi.repository import GLib

        from framewise import scrollshot
        loop, out = GLib.MainLoop(), {}

        def done(picture, note):
            out["picture"], out["note"] = picture, note
            loop.quit()
        with mock.patch.object(scrollshot, "POLL_MS", 1):
            scrollshot.ScrollCapture(shell, (0, 0, 500, shell.view), done)
            GLib.timeout_add_seconds(60, loop.quit)
            loop.run()
        return out

    def test_smooth_scrolling_page_is_captured_to_the_end(self):
        # 2g.2: a browser page used to stop after one scroll.
        p = page(height=4000)
        out = self.run_capture(SmoothShell(p))
        self.assertEqual(out["note"], "")
        self.assertEqual(out["picture"].shape[0], p.shape[0])
        self.assertTrue(np.array_equal(out["picture"], p))
