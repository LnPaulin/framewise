import unittest
import zlib

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from framewise import colors, shortcuts  # noqa: E402


def pixbuf(pixels, w, h):
    """A small RGB picture from rows of (r, g, b)."""
    data = bytes(v for row in pixels for px in row for v in px)
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB,
                                           False, 8, w, h, w * 3)


class ColorTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(colors.hex_code((53, 132, 228)), "#3584E4")
        self.assertEqual(colors.rgb_code((53, 132, 228)), "rgb(53, 132, 228)")
        self.assertEqual(colors.hex_code((0, 0, 0)), "#000000")

    def test_color_at(self):
        pb = pixbuf([[(255, 0, 0), (0, 255, 0)], [(0, 0, 255), (1, 2, 3)]], 2, 2)
        self.assertEqual(colors.color_at(pb, 1, 0), (0, 255, 0))
        self.assertEqual(colors.color_at(pb, 1, 1), (1, 2, 3))
        self.assertEqual(colors.color_at(pb, 9, -4), (0, 255, 0))      # clamped inside

    def test_color_at_with_alpha_and_padding(self):
        # Screenshots load with an alpha channel and padded rows.
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 3, 2)
        pb.fill(0x10203040)
        self.assertEqual(colors.color_at(pb, 2, 1), (0x10, 0x20, 0x30))

    def test_swatch_is_a_png(self):
        data = colors.swatch_png((53, 132, 228), size=8)
        self.assertTrue(data.startswith(b"\x89PNG"))
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        self.assertEqual((pb.get_width(), pb.get_height()), (8, 8))
        self.assertEqual(colors.color_at(pb, 4, 4), (53, 132, 228))
        self.assertTrue(zlib.crc32(data))

    def test_shortcut_listed(self):
        ids = [s[0] for s in shortcuts.SHORTCUTS]
        self.assertIn("framewise-color", ids)
        self.assertEqual(len(ids), len(set(ids)))


class PickSelectorTests(unittest.TestCase):
    """The selector's pick mode, without opening any window."""

    def test_pixel_under_pointer_on_a_scaled_screen(self):
        from framewise.shot_select import ShotSelector
        sel = ShotSelector.__new__(ShotSelector)
        sel.pick, sel.ratio, sel.origin = True, 2.0, (0, 0)    # a 200% screen
        sel.pixbuf = pixbuf([[(i, j, 0) for i in range(10)] for j in range(10)], 10, 10)
        sel.pointer = (2.5, 3.0)
        self.assertEqual(sel.pixel_at(sel.pointer), (5, 6))
        self.assertEqual(sel.color_under_pointer(), (5, 6, 0))
        results = []
        sel.finished, sel.layers, sel.on_done = False, [], results.append
        sel.finish = lambda r: results.append(r)
        sel.release((1.0, 1.5), shift=True)
        self.assertEqual(results, [("color", (2, 3, 0), True)])


if __name__ == "__main__":
    unittest.main()
