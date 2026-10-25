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


def crop_of(buf):
    """The part of the frame that holds the picture, as (x, y, w, h), from the
    crop note GNOME attaches to window recordings, or None.

    GNOME 50 sends a window as a monitor-sized frame with the window inside
    (so resizing the window needs no renegotiation) and notes where it is.
    Converters honour the note: they cut the window out, then scale it to
    the output size, so the output must be the note's size, not the frame's.
    The note's fields aren't reachable from Python, so they're read from the
    C struct: GstVideoCropMeta { GstMeta meta; guint x, y, width, height; }.
    """
    meta = buf.get_meta("GstVideoCropMetaAPI")
    if meta is None:
        return None
    try:
        import ctypes
        import re
        addr = int(re.search(r"\(void at (0x[0-9a-fA-F]+)\)", repr(meta)).group(1), 16)
        offset = ctypes.sizeof(ctypes.c_int) + ctypes.sizeof(ctypes.c_void_p)  # flags + info
        offset = (offset + 7) // 8 * 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else offset
        x, y, w, h = ctypes.cast(addr + offset, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        return (int(x), int(y), int(w), int(h))
    except (AttributeError, ValueError, TypeError):
        return None


def grab(capture, on_done, timeout=3.0, with_crop=False):
    """Calls on_done(Gdk.Texture) on the main loop, or on_done(None).
    with_crop: on_done(texture, crop), crop being crop_of() of the first
    frames GNOME sent (None if there was no note)."""
    desc = (f"pipewiresrc name=src fd={capture.fd} path={capture.stream.node_id} "
            f"client-name=framewise-still ! "
            f"{gpu.convert()} ! video/x-raw,format=BGRx ! "
            f"appsink name=sink max-buffers=1 drop=true sync=false")
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error:
        GLib.idle_add(on_done, *((None, None) if with_crop else (None,)))
        return
    crop = []

    def note_crop(_pad, info):
        buf = info.get_buffer()
        found = crop_of(buf) if buf is not None else None
        if found and not crop:
            crop.append(found)
        return Gst.PadProbeReturn.OK
    if with_crop:
        pipe.get_by_name("src").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, note_crop)

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
        if with_crop:
            GLib.idle_add(on_done, surface, crop[0] if crop else None)
        else:
            GLib.idle_add(on_done, surface)

    threading.Thread(target=work, daemon=True).start()
