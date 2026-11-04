"""Showing GStreamer video in GTK.

gtk4paintablesink (gstreamer1.0-gtk4) when it is installed. Ubuntu 24.04 has
no such package, so without it frames come out of an appsink and are shown
as textures (FrameStream): a little more work for the processor, same picture.
Frames must reach the sink as BGRA.
"""

import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gst  # noqa: E402

Gst.init(None)


def has_gtk_sink():
    return Gst.ElementFactory.find("gtk4paintablesink") is not None


def sink_desc(name, sync=True):
    """The pipeline text for the sink (ends with a space)."""
    if has_gtk_sink():
        return f"gtk4paintablesink name={name} sync={str(sync).lower()} "
    return (f"appsink name={name} emit-signals=true max-buffers=1 drop=true "
            f"sync={str(sync).lower()} ")


def paintable(pipeline, name):
    """The paintable showing what reaches the sink called name."""
    sink = pipeline.get_by_name(name)
    if sink.get_factory().get_name() == "gtk4paintablesink":
        return sink.get_property("paintable")
    stream = FrameStream()
    sink.connect("new-sample", lambda s: stream.push_sample(s.emit("pull-sample")))
    # Paused (and after a seek while paused) the frame comes as a preroll.
    sink.connect("new-preroll", lambda s: stream.push_sample(s.emit("pull-preroll")))
    return stream


def frame_array(sample):
    """A BGRA sample as an HxWx4 numpy array (a copy), or None."""
    import numpy as np
    caps = sample.get_caps().get_structure(0)
    width, height = caps.get_value("width"), caps.get_value("height")
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        raw = np.frombuffer(info.data, np.uint8)
        stride = raw.size // height
        return raw.reshape(height, stride)[:, :width * 4].reshape(height, width, 4).copy()
    finally:
        buf.unmap(info)


class FrameStream(GObject.Object, Gdk.Paintable):
    """Shows frames handed to it (a paintable, like gtk4paintablesink's).
    set_texture on the main loop; push_sample / push_frame from any thread."""

    def __init__(self):
        super().__init__()
        self._texture = None
        self._pending = None
        self._lock = threading.Lock()
        self._scheduled = False

    def push_sample(self, sample):
        if sample is not None:
            frame = frame_array(sample)
            if frame is not None:
                self.push_frame(frame)
        return Gst.FlowReturn.OK

    def push_frame(self, frame):
        """frame: HxWx4 BGRA numpy array. Only the newest one is shown."""
        with self._lock:
            self._pending = frame
            schedule = not self._scheduled
            self._scheduled = True
        if schedule:
            GLib.idle_add(self._show_pending)

    def _show_pending(self):
        with self._lock:
            frame, self._pending = self._pending, None
            self._scheduled = False
        if frame is not None:
            h, w = frame.shape[:2]
            self.set_texture(Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                                   GLib.Bytes.new(frame.tobytes()), w * 4))
        return False

    def set_texture(self, texture):
        resized = (self._texture is None or self._texture.get_width() != texture.get_width()
                   or self._texture.get_height() != texture.get_height())
        self._texture = texture
        if resized:
            self.invalidate_size()
        self.invalidate_contents()

    def do_snapshot(self, snapshot, width, height):
        if self._texture is not None:
            self._texture.snapshot(snapshot, width, height)

    def do_get_intrinsic_width(self):
        return self._texture.get_width() if self._texture else 0

    def do_get_intrinsic_height(self):
        return self._texture.get_height() if self._texture else 0

    def do_get_intrinsic_aspect_ratio(self):
        if not self._texture:
            return 0.0
        return self._texture.get_width() / max(1, self._texture.get_height())

    def do_get_current_image(self):
        return self._texture or Gdk.Paintable.new_empty(0, 0)

    def do_get_flags(self):
        return 0
