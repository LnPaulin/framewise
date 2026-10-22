"""Lock focus: the camera's autofocus is turned off and on, and put back.

Uses a fake webcam (no device is opened)."""

import unittest
from unittest import mock

from framewise import camera, webcam_controls


class FakeWebcam:
    def __init__(self, autofocus=1):
        self.values = {webcam_controls.FOCUS_AUTO: autofocus}
        self.writes = []

    def has(self, _device, control):
        return control in self.values

    def get_value(self, _device, control):
        return self.values.get(control)

    def default(self, _device, control):
        return 1 if control in self.values else None

    def set_value(self, _device, control, value):
        self.values[control] = value
        self.writes.append(value)
        return True


class FocusLockTest(unittest.TestCase):
    def setUp(self):
        self.cam_hw = FakeWebcam()
        patches = [mock.patch.object(webcam_controls, name, getattr(self.cam_hw, name))
                   for name in ("has", "get_value", "set_value", "default")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.timers = []
        timeout = mock.patch.object(camera.GLib, "timeout_add",
                                    lambda ms, fn: self.timers.append(fn) or len(self.timers))
        remove = mock.patch.object(camera.GLib, "source_remove", lambda _id: None)
        for p in (timeout, remove):
            p.start()
            self.addCleanup(p.stop)
        self.cam = camera.Camera.__new__(camera.Camera)
        camera.Camera.__init__(self.cam, None)
        self.cam.device = "/dev/fake"
        self.cam.pipeline = object()   # "running"

    def autofocus(self):
        return self.cam_hw.values[webcam_controls.FOCUS_AUTO]

    def test_ioctl_numbers_match_the_kernel(self):
        self.assertEqual(webcam_controls.VIDIOC_QUERYCTRL, 0xC0445624)
        self.assertEqual(webcam_controls.VIDIOC_G_CTRL, 0xC008561B)
        self.assertEqual(webcam_controls.VIDIOC_S_CTRL, 0xC008561C)

    def test_lock_and_unlock_while_running(self):
        self.cam.set_focus_lock(True)
        self.assertEqual(self.autofocus(), 0)
        self.cam.set_focus_lock(False)
        self.assertEqual(self.autofocus(), 1)

    def test_on_open_focuses_first_then_locks(self):
        self.cam._focus_lock = True
        self.cam._lock_after_settling()
        self.assertEqual(self.autofocus(), 1)
        self.timers[-1]()
        self.assertEqual(self.autofocus(), 0)

    def test_closing_puts_the_camera_setting_back(self):
        self.cam.set_focus_lock(True)
        self.cam.pipeline = None
        self.cam.stop()
        self.assertEqual(self.autofocus(), 1)

    def test_left_locked_by_an_earlier_run_is_undone(self):
        self.cam_hw.values[webcam_controls.FOCUS_AUTO] = 0   # e.g. after logging out mid-preview
        self.cam.set_focus_lock(True)
        self.cam.pipeline = None
        self.cam.stop()
        self.assertEqual(self.autofocus(), 1)

    def test_restart_for_effects_keeps_the_lock(self):
        self.cam.set_focus_lock(True)
        self.cam.pipeline = None
        self.cam.stop(keep_effects=True)
        self.assertEqual(self.autofocus(), 0)

    def test_camera_without_autofocus_is_left_alone(self):
        del self.cam_hw.values[webcam_controls.FOCUS_AUTO]
        self.assertFalse(self.cam.has_autofocus())
        self.cam.set_focus_lock(True)
        self.assertEqual(self.cam_hw.writes, [])

    def test_test_source_never_touches_the_device(self):
        self.cam.test_source = "videotestsrc ! "
        self.assertFalse(self.cam.has_autofocus())


if __name__ == "__main__":
    unittest.main()
