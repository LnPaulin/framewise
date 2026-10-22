import unittest

from framewise import compose


def style(kind="none", aspect="original", padding=0.45):
    s = compose.default_style()
    s["background"]["kind"] = kind
    s["background"]["padding"] = padding
    s["aspect"] = aspect
    return s


class ComposeTests(unittest.TestCase):
    def test_plain_is_untouched(self):
        lay = compose.layout(1920, 1080, style())
        self.assertEqual(lay["canvas"], (1920, 1080))
        self.assertEqual(lay["rect"], (0, 0, 1920, 1080))
        self.assertFalse(lay["background"])

    def test_shape_without_background_crops_centre(self):
        lay = compose.layout(3440, 1440, style(aspect="wide"))
        W, H = lay["canvas"]
        self.assertAlmostEqual(W / H, 16 / 9, places=2)
        x, y, w, h = lay["source"]
        self.assertAlmostEqual(x + w / 2, 0.5, places=3)
        self.assertEqual(h, 1.0)

    def test_background_grows_canvas(self):
        lay = compose.layout(1920, 1080, style(kind="preset"))
        W, H = lay["canvas"]
        x, y, w, h = lay["rect"]
        self.assertGreater(W, 1920)
        self.assertEqual((w, h), (1920, 1080))  # no loss of sharpness
        self.assertEqual(x, (W - w) // 2 // 2 * 2)

    def test_background_with_shape_and_cap(self):
        lay = compose.layout(3440, 1440, style(kind="preset", aspect="vertical"))
        W, H = lay["canvas"]
        self.assertLessEqual(max(W, H), compose.MAX_EDGE)
        self.assertAlmostEqual(W / H, 9 / 16, places=2)
        x, y, w, h = lay["rect"]
        self.assertLessEqual(x + w, W)
        self.assertLessEqual(y + h, H)
        for v in (W, H, w, h):
            self.assertEqual(v % 2, 0)

    def test_anchors(self):
        self.assertEqual(compose.anchor_point("center", (100, 100), (20, 10)), (40, 45))
        x, y = compose.anchor_point("bottomTrailing", (1000, 500), (100, 50))
        self.assertAlmostEqual(x, 880)
        self.assertAlmostEqual(y, 430)

    def test_fade(self):
        self.assertEqual(compose.fade_alpha(5, 2, 8, 0.25), 1.0)
        self.assertEqual(compose.fade_alpha(1, 2, 8, 0.25), 0.0)
        self.assertTrue(0 < compose.fade_alpha(2.1, 2, 8, 0.25) < 1)


if __name__ == "__main__":
    unittest.main()
