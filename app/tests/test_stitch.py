import unittest

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
