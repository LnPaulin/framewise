"""Grabs one still picture from an open screen share.

The area selector shows this picture (dimmed) instead of trying to be a
see-through window, which GNOME shows as black for full-screen windows.
"""

import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gst  # noqa: E402

from . import gpu  # noqa: E402

Gst.init(None)


def grab(capture, on_done, timeout=3.0):
    """Calls on_done(Gdk.Texture) on the main loop, or on_done(None)."""
    desc = (f"pipewiresrc fd={capture.fd} path={capture.stream.node_id} "
            f"client-name=framewise-still ! "
            f"{gpu.convert()} ! video/x-raw,format=BGRx ! "
            f"appsink name=sink max-buffers=1 drop=true sync=false")
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error:
        GLib.idle_add(on_done, None)
        return

    def work():
        surface = None
        try:
            pipe.set_state(Gst.State.PLAYING)
            sample = pipe.get_by_name("sink").emit("try-pull-sample", int(timeout * Gst.SECOND))
            if sample is not None:
                caps = sample.get_caps().get_structure(0)
                w, h = caps.get_value("width"), caps.get_value("height")
                buf = sample.get_buffer()
                ok, info = buf.map(Gst.MapFlags.READ)
                if ok:
                    data = GLib.Bytes.new(bytes(info.data))
                    buf.unmap(info)
                    stride = data.get_size() // h
                    surface = Gdk.MemoryTexture.new(
                        w, h, Gdk.MemoryFormat.B8G8R8X8, data, stride)
        except Exception:
            surface = None
        finally:
            pipe.set_state(Gst.State.NULL)
        GLib.idle_add(on_done, surface)

    threading.Thread(target=work, daemon=True).start()
