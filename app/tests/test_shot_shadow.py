import unittest

import cairo
import numpy as np

from framewise import shot_draw as sd


class ShadowTests(unittest.TestCase):
    def test_fades_with_no_edge_line(self):
        W, H = 900, 400
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
        cr = cairo.Context(surf)
        cr.set_source_rgb(0.4, 0.15, 0.3)
        cr.paint()
        sd._paint_shadow(cr, 100, 20, 700, 300, 24, 300)
        surf.flush()
        a = np.frombuffer(surf.get_data(), np.uint8).reshape(H, -1, 4)[:, :W, 2].astype(int)
        below = a[320:, 450]                     # straight down from the picture's bottom edge
        self.assertLess(below[0], below[-1] - 20)  # there is a shadow...
        self.assertTrue(np.all(np.diff(below) >= 0))   # ...that only gets lighter...
        # ...and eases out: the fade never stops suddenly (a sudden stop
        # reads as a line along the shadow's edge).
        self.assertLessEqual(np.abs(np.diff(below, 2)).max(), 2)
        self.assertEqual(below[-1], a[-1, 5])    # and ends at the background


if __name__ == "__main__":
    unittest.main()
