import subprocess
import tempfile
import unittest
from pathlib import Path

from framewise import controls_patch as cp
from framewise import edit_model as em
from framewise import export, gpu


class GeometryTests(unittest.TestCase):
    def test_rect_on_monitor_with_padding(self):
        fr = cp.rect_on_monitor((900, 0, 100, 32), (0, 0, 2000, 1000))
        self.assertAlmostEqual(fr[0], (900 - cp.PAD - cp.LEFT_ROOM) / 2000)
        self.assertEqual(fr[1], 0)                      # clipped at the top
        self.assertAlmostEqual(fr[2], (100 + 2 * cp.PAD + cp.LEFT_ROOM) / 2000)

    def test_other_monitor_is_none(self):
        self.assertIsNone(cp.rect_on_monitor((900, 0, 100, 32), (2000, 0, 1920, 1080)))
        self.assertIsNone(cp.rect_on_monitor((0, 0, 0, 0), (0, 0, 1920, 1080)))

    def test_monitor_offset(self):
        fr = cp.rect_on_monitor((2900, 0, 100, 32), (2000, 0, 2000, 1000))
        self.assertAlmostEqual(fr[0], (900 - cp.PAD - cp.LEFT_ROOM) / 2000)

    def test_pixels_even_and_inside(self):
        info = {"controls": {"rect": [0.4501, 0.0, 0.0523, 0.0377]}}
        x, y, w, h = cp.pixels(info, (3440, 1440))
        for v in (x, y, w, h):
            self.assertEqual(v % 2, 0)
        self.assertLessEqual(x, 0.4501 * 3440)
        self.assertGreaterEqual(x + w, (0.4501 + 0.0523) * 3440)
        self.assertGreaterEqual(y + h, 0.0377 * 1440)

    def test_outside_area_is_none(self):
        info = {"controls": {"rect": [0.45, 0.0, 0.05, 0.03]}}
        self.assertIsNone(cp.pixels(info, (1920, 1080), (0.0, 0.5, 0.5, 0.5)))
        self.assertIsNone(cp.in_area(info, (0.0, 0.5, 0.5, 0.5)))
        self.assertIsNotNone(cp.pixels(info, (1920, 1080), (0.4, 0.0, 0.5, 0.5)))

    def test_in_area_fractions(self):
        info = {"controls": {"rect": [0.5, 0.0, 0.1, 0.05]}}
        x, y, w, h = cp.in_area(info, (0.25, 0.0, 0.5, 0.5))
        self.assertAlmostEqual(x, 0.5)
        self.assertAlmostEqual(w, 0.2)
        self.assertAlmostEqual(h, 0.1)

    def test_no_controls(self):
        self.assertIsNone(cp.pixels({}, (100, 100)))
        self.assertIsNone(cp.pixels(None, (100, 100)))
        self.assertIsNone(cp.info_for((0, 0, 0, 0), (0, 0, 10, 10)))


def _grey_at(path, t, x, y, w, h):
    """Mean grey level of a region of the frame at t."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                          "-vf", f"crop={w}:{h}:{x}:{y},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return sum(raw) / len(raw)


class TakeCase(unittest.TestCase):
    """A white box (the controls) appears at 0.5 s on a grey picture; the
    saved video must stay grey there."""

    BOX = (100, 0, 80, 20)
    SIZE = (640, 360)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        x, y, w, h = self.BOX
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=gray:s={self.SIZE[0]}x{self.SIZE[1]}:r=30:d=2",
                        "-vf", f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill:enable='gte(t,0.5)'",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.dir / "take.mkv")],
                       check=True)
        rect = cp.rect_on_monitor(self.BOX, (0, 0, *self.SIZE))
        self.info = {"fps": 30, "controls": {"rect": rect, "clean_at": 0.1}}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)



class CoverTests(TakeCase):
    def test_box_is_there_before(self):
        self.assertGreater(_grey_at(self.dir / "take.mkv", 1.5, *self.BOX), 200)

    def _export(self, hw, crop_fraction=None):
        file = cp.patch_file(self.dir, self.info, self.SIZE, crop_fraction)
        self.assertIsNotNone(file)
        patch = {"file": file, "px": cp.pixels(self.info, self.SIZE, crop_fraction)}
        doc = em.normalize(em.new_document(2.0), 2.0)
        crop = export.crop_pixels(crop_fraction, self.SIZE) if crop_fraction else None
        out = self.dir / "out.mp4"
        cmd = export.build_edit_command(self.dir / "take.mkv", out, doc, [], crop, fps=30,
                                        duration=2.0, hw=hw, patch=patch)
        subprocess.run(cmd, check=True, capture_output=True)
        return out, crop

    def test_covered_in_software(self):
        out, _ = self._export(False)
        self.assertLess(_grey_at(out, 1.5, *self.BOX), 150)

    def test_covered_with_area(self):
        out, crop = self._export(False, (0.0, 0.0, 0.5, 0.5))
        x, y, w, h = self.BOX
        self.assertLess(_grey_at(out, 1.5, x - crop[0], y - crop[1], w, h), 150)

    @unittest.skipUnless(gpu.ffmpeg_vaapi(), "no VA-API")
    def test_covered_on_gpu(self):
        out, _ = self._export(True)
        self.assertLess(_grey_at(out, 1.5, *self.BOX), 150)


if __name__ == "__main__":
    unittest.main()


class SaveTests(TakeCase):
    """Through save_async, as the app saves: unedited and with a blur (the
    composed path)."""

    def _save(self, doc):
        from gi.repository import GLib
        loop, result = GLib.MainLoop(), {}

        def done(ok, message):
            result.update(ok=ok, message=message)
            loop.quit()
        dest = self.dir / "saved.mp4"
        export.save_async(self.dir / "take.mkv", dest, None, done, doc=doc,
                          take_info=self.info, duration=2.0)
        GLib.timeout_add_seconds(60, loop.quit)
        loop.run()
        self.assertTrue(result.get("ok"), result)
        return dest

    def test_unedited_is_covered(self):
        doc = em.normalize(em.new_document(2.0), 2.0)
        doc["zoom"]["enabled"] = False
        out = self._save(doc)
        self.assertLess(_grey_at(out, 1.5, *self.BOX), 150)

    def test_composed_is_covered(self):
        doc = em.new_document(2.0)
        doc["zoom"]["enabled"] = False
        doc["blurs"] = [{"id": "b", "start": 0.0, "end": 2.0, "rect": [0.6, 0.6, 0.2, 0.2], "strength": 0.5}]
        doc = em.normalize(doc, 2.0)
        out = self._save(doc)
        self.assertLess(_grey_at(out, 1.5, *self.BOX), 150)
