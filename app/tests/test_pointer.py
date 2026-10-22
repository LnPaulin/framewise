import unittest

from framewise import pointer


def samples_line(duration, fps=60, click_at=None):
    out = []
    for i in range(int(duration * fps)):
        t = i / fps
        x = 0.2 + 0.6 * t / duration
        down = 1 if click_at and click_at <= t < click_at + 0.1 else 0
        out.append((t, x, 0.5, down, 1))
    return out


class PointerTests(unittest.TestCase):
    def test_clicks(self):
        s = samples_line(2, click_at=1.0)
        clicks = pointer.clicks_from_samples(s)
        self.assertEqual([d for _t, d in clicks], [True, False])

    def test_smoothing_is_exact_at_clicks(self):
        # A zig-zag path: smoothing changes it, except at the click.
        s = [(i / 60, 0.5 + (0.05 if (i // 6) % 2 else -0.05), 0.5, 1 if 60 <= i < 66 else 0, 1)
             for i in range(120)]
        raw, _ = pointer.frames(s, 120, 60)
        clicks = pointer.clicks_from_samples(s)
        smooth = pointer.smoothed(raw, clicks, "smooth", 60)
        self.assertAlmostEqual(smooth[62][0], raw[62][0], places=6)   # while pressed
        self.assertGreater(abs(smooth[30][0] - raw[30][0]), 0.005)      # elsewhere it differs

    def test_idle_fades_out_and_back(self):
        s = [(0.0, 0.5, 0.5, 0, 1), (5.0, 0.5, 0.5, 0, 1), (5.5, 0.8, 0.5, 0, 1), (6.0, 0.8, 0.5, 0, 1)]
        raw, _ = pointer.frames(s, 360, 60)
        op = pointer.idle_opacity(raw, 60)
        self.assertEqual(op[60], 1.0)          # 1 s still: shown
        self.assertLess(op[200], 0.01)         # 3.3 s still: hidden
        self.assertGreater(op[299], 0.9)       # comes back before moving at 5 s

    def test_press_and_ring(self):
        clicks = [(1.0, True), (1.1, False)]
        self.assertAlmostEqual(pointer.press_scale(1.05, clicks), pointer.PRESS_SCALE)
        self.assertEqual(pointer.press_scale(0.5, clicks), 1.0)
        r = pointer.ring(1.2, clicks, lambda t: (0.3, 0.4))
        self.assertAlmostEqual(r[0], 0.5)
        self.assertEqual(r[1:], (0.3, 0.4))
        self.assertIsNone(pointer.ring(1.5, clicks, lambda t: (0, 0)))

    def test_track_hides_outside_area(self):
        s = [(0.0, 1.5, 0.5, 0, 1), (1.0, 1.5, 0.5, 0, 1)]
        frames, rings = pointer.track(s, 1.0, 30)
        self.assertEqual(frames[10][2], 0.0)

    def test_pixel_size(self):
        self.assertEqual(pointer.pixel_size(1440), 40)
        self.assertEqual(pointer.pixel_size(600), 24)


if __name__ == "__main__":
    unittest.main()
