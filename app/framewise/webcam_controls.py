"""Reads and changes a webcam's own settings (here: autofocus).

Uses the camera driver's standard controls (V4L2), the same ones v4l2-ctl,
OBS and Cameractrls change. Reading or changing them doesn't turn the camera
on, and it works while the camera is in use.
"""

import fcntl
import os
import struct

# Control ids (linux/v4l2-controls.h).
FOCUS_AUTO = 0x009A090C          # continuous autofocus on/off

_FLAG_DISABLED = 0x0001


def _iowr(nr, size):
    """The _IOWR('V', nr, size) request number."""
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


# struct v4l2_queryctrl: id, type, name[32], min, max, step, default, flags, reserved[2]
_QUERYCTRL = struct.Struct("=II32siiiiI8x")
# struct v4l2_control: id, value
_CONTROL = struct.Struct("=Ii")
VIDIOC_QUERYCTRL = _iowr(36, _QUERYCTRL.size)
VIDIOC_G_CTRL = _iowr(27, _CONTROL.size)
VIDIOC_S_CTRL = _iowr(28, _CONTROL.size)


def _open(device):
    return os.open(device, os.O_RDWR | os.O_NONBLOCK)


def has(device, control):
    """The camera has this control (and it isn't disabled)."""
    try:
        fd = _open(device)
    except OSError:
        return False
    try:
        buf = bytearray(_QUERYCTRL.pack(control, 0, b"", 0, 0, 0, 0, 0))
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
        flags = _QUERYCTRL.unpack(buf)[7]
        return not flags & _FLAG_DISABLED
    except OSError:
        return False
    finally:
        os.close(fd)


def default(device, control):
    """The control's factory default, or None."""
    try:
        fd = _open(device)
    except OSError:
        return None
    try:
        buf = bytearray(_QUERYCTRL.pack(control, 0, b"", 0, 0, 0, 0, 0))
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
        return _QUERYCTRL.unpack(buf)[6]
    except OSError:
        return None
    finally:
        os.close(fd)


def get_value(device, control):
    """The control's value, or None if it can't be read."""
    try:
        fd = _open(device)
    except OSError:
        return None
    try:
        buf = bytearray(_CONTROL.pack(control, 0))
        fcntl.ioctl(fd, VIDIOC_G_CTRL, buf)
        return _CONTROL.unpack(buf)[1]
    except OSError:
        return None
    finally:
        os.close(fd)


def set_value(device, control, value):
    """Changes a control; True if the camera took it."""
    try:
        fd = _open(device)
    except OSError:
        return False
    try:
        fcntl.ioctl(fd, VIDIOC_S_CTRL, bytearray(_CONTROL.pack(control, int(value))))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
