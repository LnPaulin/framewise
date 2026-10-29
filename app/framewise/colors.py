"""Color picker: the color of one pixel on the frozen screen, copied as text.

A click copies the hex code (#3584E4); Shift-click copies rgb(53, 132, 228).
The notification shows a swatch of the color.
"""

import struct
import zlib

from gi.repository import Gio, GLib


def color_at(pixbuf, x, y):
    """(r, g, b) of the pixel at x, y (clamped inside the picture)."""
    x = max(0, min(pixbuf.get_width() - 1, int(x)))
    y = max(0, min(pixbuf.get_height() - 1, int(y)))
    data = pixbuf.get_pixels()
    i = y * pixbuf.get_rowstride() + x * pixbuf.get_n_channels()
    return data[i], data[i + 1], data[i + 2]


def hex_code(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def rgb_code(rgb):
    return "rgb({}, {}, {})".format(*rgb)


def swatch_png(rgb, size=64):
    """A small square PNG of the color (for the notification's icon)."""
    row = b"\x00" + bytes(rgb) * size
    raw = row * size

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def copy_and_notify(app, rgb, as_rgb=False):
    from . import wlcopy
    text = rgb_code(rgb) if as_rgb else hex_code(rgb)
    other = hex_code(rgb) if as_rgb else rgb_code(rgb)
    copied = wlcopy.copy(text.encode(), "text/plain;charset=utf-8")
    n = Gio.Notification.new(f"Copied {text}" if copied else text)
    n.set_body(other if copied else "Couldn't copy: wl-copy (package wl-clipboard) is not installed")
    n.set_icon(Gio.BytesIcon.new(GLib.Bytes.new(swatch_png(rgb))))
    app.send_notification("color", n)
    return copied
