import time
import unittest

from framewise import camera_fx

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None


@unittest.skipUnless(camera_fx.available(), "needs python3-opencv")
class EffectsTests(unittest.TestCase):
    def setUp(self):
        self.fx = camera_fx.Effects()

    def tearDown(self):
        self.fx.stop()

    def frame(self):
        f = np.zeros((540, 960, 4), np.uint8)
        f[..., :3] = np.random.default_rng(1).integers(0, 255, (540, 960, 3), dtype=np.uint8)
        f[..., 3] = 255
        return f

    def test_off_is_untouched(self):
        f = self.frame()
        self.assertTrue(np.array_equal(self.fx.process(f.copy()), f))

    def test_blur_changes_frame_and_keeps_alpha(self):
        self.fx.blur = 1.0
        f = self.frame()
        for _ in range(10):        # let the model thread produce a mask
            self.fx.process(f.copy())
            time.sleep(0.05)
        out = self.fx.process(f.copy())
        self.assertEqual(out.shape, f.shape)
        self.assertFalse(np.array_equal(out, f))
        self.assertTrue((out[..., 3] == 255).all())

    def test_soft_ellipse(self):
        m = camera_fx._soft_ellipse((100, 200), (100, 50), (40, 30), 0.3)
        self.assertEqual(m.shape, (100, 200))
        self.assertGreater(m[50, 100], 0.99)
        self.assertLess(m[0, 0], 0.01)


if __name__ == "__main__":
    unittest.main()
