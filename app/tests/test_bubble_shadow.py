import re
import unittest

from framewise import bubble


class BubbleShadowTests(unittest.TestCase):
    def test_shadow_fits_in_the_window(self):
        # Past the window's edge the shadow is cut off, which shows as a line.
        css = bubble.CSS
        margin = int(re.search(r"\.ck-cam-frame \{[^}]*margin: (\d+)px", css).group(1))
        self.assertEqual(margin, bubble.FRAME_MARGIN)
        x, y, blur = map(int, re.search(r"\.ck-cam-frame \{[^}]*box-shadow: (-?\d+)p?x? (-?\d+)px (\d+)px",
                                        css).groups())
        self.assertLessEqual(abs(y) + blur, margin)
        self.assertLessEqual(abs(x) + blur, margin)


if __name__ == "__main__":
    unittest.main()
