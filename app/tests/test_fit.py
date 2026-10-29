import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gi.repository import GLib

from framewise import fit, gpu


def make_video(path, seconds=12, size="1920x1080", fps=60, extra=()):
    """A busy test video (noise is hard to compress, so it starts big)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-vf", "noise=alls=40:allf=t", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
                    "-c:a", "aac", *extra, str(path)], check=True)
    return path


def run_fit(info, mb, hw):
    loop, out = GLib.MainLoop(), {}

    def done(ok, message):
        out.update(ok=ok, message=message)
        loop.quit()
    fit.fit_async(info, mb, lambda f: out.setdefault("progress", []).append(f), done, hw=hw)
    GLib.timeout_add_seconds(240, loop.quit)
    loop.run()
    return out


class FitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.video = make_video(cls.dir / "Demo video.mp4")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_info(self):
        info = fit.Info(self.video)
        self.assertEqual((info.width, info.height), (1920, 1080))
        self.assertAlmostEqual(info.duration, 12, delta=0.2)
        self.assertAlmostEqual(info.fps, 60, delta=0.1)
        self.assertTrue(info.has_audio)
        self.assertGreater(info.megabytes, 5)

    def _check(self, hw):
        info = fit.Info(self.video)
        out = run_fit(info, 2, hw)
        self.assertTrue(out.get("ok"), out)
        result = Path(out["message"])
        self.assertTrue(result.name.startswith("Demo video (2 MB)"))
        self.assertLessEqual(result.stat().st_size, 2_000_000)
        made = fit.Info(result)
        self.assertAlmostEqual(made.duration, 12, delta=0.3)
        self.assertLessEqual(made.fps, 30.5)
        self.assertTrue(made.has_audio)
        self.assertTrue(out.get("progress"))
        self.assertFalse(list(self.dir.glob(".*making*")))
        result.unlink()

    def test_fit_in_software(self):
        self._check(False)

    @unittest.skipUnless(gpu.ffmpeg_vaapi(), "no VA-API")
    def test_fit_on_gpu(self):
        self._check(True)

    def test_names_never_replace(self):
        a = fit.destination(self.video, 10)
        a.write_bytes(b"x")
        b = fit.destination(self.video, 10)
        self.assertNotEqual(a, b)
        self.assertEqual(b.name, "Demo video (10 MB) 2.mp4")
        a.unlink()

    def test_too_long(self):
        info = fit.Info(self.video)
        info.duration = 3 * 3600            # three hours can't fit in 1 MB
        from framewise import export
        self.assertIsNone(export.fit_plan(info.duration, (info.width, info.height), info.fps, True, 1))
        self.assertIn("too long", fit.too_long_message(info, 1))


class RotatedTests(unittest.TestCase):
    def test_phone_video_turned(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src = make_video(tmp / "plain.mp4", seconds=1, size="640x360", fps=30)
            rotated = tmp / "phone.mp4"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(src),
                                "-c", "copy", str(rotated)], capture_output=True)
            if r.returncode:
                self.skipTest("this ffmpeg can't write rotation")
            info = fit.Info(rotated)
            self.assertEqual((info.width, info.height), (360, 640))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NotAVideoTests(unittest.TestCase):
    def test_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(b"not a video")
            f.flush()
            with self.assertRaises((ValueError, KeyError)):
                fit.Info(f.name)


if __name__ == "__main__":
    unittest.main()
