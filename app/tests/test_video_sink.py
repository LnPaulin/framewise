"""Video shows in GTK with and without gtk4paintablesink (Ubuntu 24.04 has
none). Uses a fake webcam and a made-up recording."""

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import GLib, Gst

from framewise import camera, devices, player, video_sink


def run_until(check, seconds=10):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)
    return check()


def shows_picture(paintable):
    return paintable.get_intrinsic_width() > 0


class Both:
    """Each test runs once with the GTK sink (when installed) and once without."""
    gtk_sink = None

    def setUp(self):
        if self.gtk_sink and not Gst.ElementFactory.find("gtk4paintablesink"):
            self.skipTest("gtk4paintablesink is not installed")
        if self.gtk_sink is False:
            p = mock.patch.object(video_sink, "has_gtk_sink", return_value=False)
            p.start()
            self.addCleanup(p.stop)

    def test_camera_shows_picture(self):
        fake = devices.Device("/dev/fake", "Fake", True, None)
        with mock.patch.object(devices, "find_camera", return_value=fake):
            cam = camera.Camera()
        cam.test_source = "videotestsrc is-live=true ! "
        self.addCleanup(cam.stop)
        cam.start()
        self.assertTrue(run_until(lambda: shows_picture(cam.paintable)))
        self.assertEqual(cam.paintable.get_intrinsic_width(), camera.Camera.WIDTH)
        if self.gtk_sink is False:
            self.assertIsInstance(cam.paintable, video_sink.FrameStream)

    def test_player_shows_first_frame_when_paused(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        take = d / "take.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(take)], check=True)
        p = player.Player(take, (640, 360))
        self.addCleanup(p.pipeline.set_state, Gst.State.NULL)
        self.assertTrue(run_until(lambda: shows_picture(p.paintable)))
        self.assertEqual(p.paintable.get_intrinsic_width(), 640)
        # A seek while paused shows that frame too.
        if self.gtk_sink is False:
            before = p.paintable.get_current_image()
            p.seek(0.5)
            self.assertTrue(run_until(lambda: p.paintable.get_current_image() is not before))


class WithGtkSink(Both, unittest.TestCase):
    gtk_sink = True


class WithoutGtkSink(Both, unittest.TestCase):
    gtk_sink = False


if __name__ == "__main__":
    unittest.main()
