"""Camera photos at the camera's biggest size.

Uses a fake webcam (GStreamer's test picture): the real one is never opened."""

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from framewise import camera, camera_fx, devices  # noqa: E402

FAKE = "videotestsrc is-live=true pattern=smpte ! "


def run_until(check, seconds=10):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)
    return check()


class SizeTests(unittest.TestCase):
    def test_biggest_in_the_bubbles_shape(self):
        caps = Gst.Caps.from_string(
            "image/jpeg,width=1280,height=720; image/jpeg,width=2560,height=1920; "
            "image/jpeg,width=2560,height=1440; video/x-raw,format=YUY2,width=2560,height=1440; "
            "image/jpeg,width=640,height=480")
        self.assertEqual(devices.photo_size(caps), ("jpeg", 2560, 1440))

    def test_other_shape_when_none_fits(self):
        caps = Gst.Caps.from_string("image/jpeg,width=640,height=480; video/x-raw,width=1600,height=1200")
        self.assertEqual(devices.photo_size(caps), ("raw", 1600, 1200))

    def test_no_fixed_sizes(self):
        self.assertIsNone(devices.photo_size(Gst.Caps.from_string("video/x-raw,width=[1,4000],height=[1,3000]")))
        self.assertIsNone(devices.photo_size(None))


class PhotoTests(unittest.TestCase):
    def make(self, photo=("jpeg", 1920, 1080)):
        fake = devices.Device("/dev/fake", "Fake", True, photo)
        with mock.patch.object(devices, "find_camera", return_value=fake):
            cam = camera.Camera()
        cam.test_source = FAKE
        self.addCleanup(cam.stop)
        self.paintables = []
        cam.on_paintable = self.paintables.append
        return cam

    def take(self, cam, frozen=None):
        got = []
        cam.take_photo(frozen, got.append)
        self.assertTrue(run_until(lambda: got), "no answer")
        return got[0]

    def test_full_size_photo_then_live_again(self):
        cam = self.make()
        cam.start()
        self.assertTrue(cam.can_take_big_photo())
        texture = self.take(cam)
        self.assertEqual((texture.get_width(), texture.get_height()), (1920, 1080))
        self.assertIsNotNone(cam.pipeline)          # the live picture is back
        self.assertIsNone(cam._still)
        self.assertIs(self.paintables[-1], cam.paintable)
        self.assertTrue(cam.can_take_big_photo())   # and another one can be taken

    def test_frozen_picture_shown_meanwhile(self):
        cam = self.make()
        cam.start()
        frozen = mock.Mock()
        frozen.get_width.return_value, frozen.get_height.return_value = 1280, 720
        shown = []
        cam.on_paintable = lambda p: shown.append(getattr(p, "_texture", p))
        with mock.patch.object(camera.FrameStream, "set_texture", lambda self, t: setattr(self, "_texture", t)):
            self.take(cam, frozen)
        self.assertIs(shown[0], frozen)

    @unittest.skipUnless(camera_fx.available(), "needs OpenCV and the models")
    def test_with_effects(self):
        cam = self.make()
        cam.set_effects(smooth=0.5, light=0.3)
        cam.start()
        self.assertTrue(cam._effects_on)
        texture = self.take(cam)
        self.assertEqual((texture.get_width(), texture.get_height()), (1920, 1080))
        self.assertTrue(cam._effects_on)
        self.assertIsNotNone(cam.pipeline)

    def test_no_bigger_size(self):
        cam = self.make(photo=("jpeg", 1280, 720))
        cam.start()
        self.assertFalse(cam.can_take_big_photo())
        self.assertIsNone(self.take(cam))

    def test_closed_while_taking(self):
        cam = self.make()
        cam.start()
        got = []
        cam.take_photo(None, got.append)
        cam.stop()
        run_until(lambda: got, 1.5)
        self.assertEqual(got, [])
        self.assertIsNone(cam.pipeline)
        self.assertIsNone(cam._still)

    def test_camera_that_fails_gives_none(self):
        cam = self.make()
        cam.start()
        # The photo's pipeline can't be made; the live one after it can.
        with mock.patch.object(cam, "_source", side_effect=["nosuchelement ! ", FAKE]):
            self.assertIsNone(self.take(cam))
        self.assertIsNotNone(cam.pipeline)


class BubbleTests(unittest.TestCase):
    """The bubble's photo button, on a stand-in bubble (no window)."""

    def test_button_saves_big_jpeg(self):
        from framewise import bubble, sounds
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        cam = PhotoTests.make(self)
        cam.start()
        shown = Gdk.MemoryTexture.new(16, 9, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                      GLib.Bytes.new(bytes(16 * 9 * 4)), 16 * 4)
        b = mock.Mock(camera=cam, _photo_busy=False)
        b._current_frame.return_value = shown
        b.get_application.return_value.session = None
        b._save_photo = lambda t: bubble.CameraBubble._save_photo(b, t)
        with mock.patch.object(bubble.GLib, "get_user_special_dir", return_value=str(d)), \
                mock.patch.object(sounds, "play"):
            bubble.CameraBubble._take_photo(b)
            self.assertTrue(run_until(lambda: list(d.glob("Photo *.jpg"))))
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file(str(next(d.glob("Photo *.jpg"))))
        self.assertEqual((pb.get_width(), pb.get_height()), (1920, 1080))
        self.assertFalse(b._photo_busy)
        b.photo_button.set_sensitive.assert_called_with(True)


class StillEffectsTests(unittest.TestCase):
    @unittest.skipUnless(camera_fx.available(), "needs OpenCV and the models")
    def test_any_size(self):
        import numpy as np
        fx = camera_fx.Effects()
        self.addCleanup(fx.stop)
        fx.blur, fx.smooth, fx.light = 0.5, 0.5, 0.5
        frame = np.full((1440, 2560, 4), 120, np.uint8)
        out = fx.still(frame)
        self.assertEqual(out.shape, frame.shape)


class SaveTests(unittest.TestCase):
    def test_jpeg(self):
        from framewise import bubble
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        data = GLib.Bytes.new(bytes([0, 0, 255, 255]) * (64 * 32))
        texture = Gdk.MemoryTexture.new(64, 32, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data, 64 * 4)
        bubble.save_jpeg(texture, d / "p.jpg")
        raw = (d / "p.jpg").read_bytes()
        self.assertEqual(raw[:3], b"\xff\xd8\xff")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file(str(d / "p.jpg"))
        self.assertEqual((pb.get_width(), pb.get_height()), (64, 32))
        px = pb.get_pixels()
        self.assertGreater(px[0], 200)       # red stays red
        self.assertLess(px[2], 60)


if __name__ == "__main__":
    unittest.main()
