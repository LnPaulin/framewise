"""Webcam feed with filters, for the floating camera bubble.

Pipeline: MJPEG from the webcam -> decoded on the Intel GPU -> filters on the
GPU (vapostproc: brightness, contrast, saturation, skin tone, noise
reduction, soft focus, mirror) -> shown in GTK.

The "Teams look" (auto light) watches how bright the picture is a few times
a second and lifts brightness and contrast smoothly when the room is dark,
the way video-call apps do low-light correction.
"""

import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gst, GstVideo  # noqa: E402

from . import gpu  # noqa: E402

Gst.init(None)

# vapostproc's sharpen: 44 is neutral, lower values blur (soft focus).
SHARPEN_NEUTRAL = 44.0

# Presets: the filters each one turns on, and where it puts the sliders.
#   auto: automatic low-light correction    skin: skin-tone enhancement 0-9
#   denoise: noise reduction 0-64           soft: soft focus 0-1
#   brightness -50..50, contrast 0.5..2, saturation 0..2
PRESETS = {
    "natural": dict(label="Natural", hint="No correction.",
                    auto=False, skin=3, denoise=0, soft=0.0,
                    brightness=0.0, contrast=1.0, saturation=1.0),
    "teams": dict(label="Auto Light", hint="Automatic low-light correction with a light touch-up.",
                  auto=True, skin=5, denoise=8, soft=0.2,
                  brightness=0.0, contrast=1.0, saturation=1.05),
    "touchup": dict(label="Retouch", hint="Low-light correction with soft focus for smoother skin.",
                    auto=True, skin=6, denoise=12, soft=0.6,
                    brightness=2.0, contrast=0.95, saturation=1.0),
    "studio": dict(label="Studio", hint="Even lighting with added contrast.",
                   auto=True, skin=5, denoise=6, soft=0.25,
                   brightness=0.0, contrast=1.15, saturation=0.95),
    "vivid": dict(label="Vivid", hint="Brighter colors and more contrast.",
                  auto=True, skin=4, denoise=4, soft=0.0,
                  brightness=0.0, contrast=1.1, saturation=1.35),
    "mono": dict(label="Mono", hint="Black and white.",
                 auto=True, skin=0, denoise=6, soft=0.0,
                 brightness=0.0, contrast=1.15, saturation=0.0),
}
DEFAULT_PRESET = "teams"
# Names used before presets existed.
OLD_LOOKS = {"bright": "teams", "soft": "touchup"}


# With effects on, frames are processed at this size (the bubble is smaller,
# and it keeps the effects quick).
FX_WIDTH, FX_HEIGHT = 960, 540


class FrameStream(GObject.Object, Gdk.Paintable):
    """Shows the frames the effects produce (a paintable, like gtk4paintablesink's)."""

    def __init__(self):
        super().__init__()
        self._texture = None

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


class Camera:
    """Runs the webcam pipeline and exposes a GDK paintable to show it."""

    WIDTH, HEIGHT, FPS = 1280, 720, 30

    def __init__(self, device_id=None):
        """device_id: the chosen camera (/dev/videoN); falls back to the first one."""
        from .devices import find_camera
        cam = find_camera(device_id)
        self.device = cam.id if cam else "/dev/video0"
        self.mjpeg = cam.mjpeg if cam else True
        self.pipeline = None
        self.paintable = None
        self.on_error = None
        self._fx = None
        self._flip = None
        self._auto_light = False
        self._auto_gain = 0.0          # current extra brightness from auto light
        self._manual = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0}
        self._skin = 3.0
        self._denoise = 0.0
        self._soft = 0.0
        self._mirror = True
        self._meter_timer = None
        # Effects (camera_fx): on when any of these is above zero.
        self._effect_values = {"blur": 0.0, "smooth": 0.0, "light": 0.0}
        self._effects = None          # camera_fx.Effects while effects are on
        self._effects_on = False      # how the running pipeline was built
        self._pending = None          # the newest processed frame, for the main loop
        self._pending_lock = threading.Lock()
        self._show_scheduled = False
        self.on_paintable = None      # called with the new paintable when it changes

    # Tests set this to a GStreamer source (e.g. a still picture) so they never
    # turn on the real webcam. It must end with "! ".
    test_source = None

    def start(self):
        if self.test_source:
            source = self.test_source
        elif self.mjpeg:
            # Most webcams send 720p only as JPEG; decode it on the GPU when we can.
            decoder = gpu.jpeg_decoder()
            source = (f"v4l2src device={self.device} ! "
                      f"image/jpeg,width={self.WIDTH},height={self.HEIGHT},framerate={self.FPS}/1 ! "
                      f"{decoder} ! ")
        else:
            source = (f"v4l2src device={self.device} ! videoconvert ! videoscale ! "
                      f"video/x-raw,width={self.WIDTH},height={self.HEIGHT} ! ")
        self._effects_on = self._want_effects()
        if self._effects_on:
            # Frames come to Python, get the effects, and are shown by FrameStream.
            width, height = FX_WIDTH, FX_HEIGHT
            view = ("appsink name=fxsink emit-signals=true max-buffers=1 drop=true sync=false "
                    f"caps=video/x-raw,format=BGRA,width={width},height={height} ")
        else:
            width, height = self.WIDTH, self.HEIGHT
            view = "gtk4paintablesink name=view sync=false "
        if gpu.gst_va():
            filters = "vapostproc name=fx disable-passthrough=true ! "
        else:
            # Software: brightness, contrast and colour, and the mirror.
            filters = ("videoconvert ! videoflip name=flip ! videoscale ! videoconvert ! "
                       "videobalance name=fx ! videoconvert ! ")
        desc = (
            source + filters +
            # Pin the full size here: otherwise the light meter's tiny
            # 64x36 branch can win the size negotiation for the whole tee.
            f"video/x-raw,format=BGRA,width={width},height={height} ! tee name=t "
            f"t. ! queue max-size-buffers=2 leaky=downstream ! {view}"
            f"t. ! queue max-size-buffers=1 leaky=downstream ! videorate drop-only=true ! "
            f"video/x-raw,framerate=4/1 ! videoscale ! videoconvert ! "
            f"video/x-raw,format=GRAY8,width=64,height=36 ! "
            f"appsink name=meter max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(desc)
        self._fx = self.pipeline.get_by_name("fx")
        self._flip = self.pipeline.get_by_name("flip")
        if self._effects_on:
            from .camera_fx import Effects
            if self._effects is None:
                self._effects = Effects()
            self._update_effects()
            self.paintable = FrameStream()
            self.pipeline.get_by_name("fxsink").connect("new-sample", self._on_fx_sample)
        else:
            self.paintable = self.pipeline.get_by_name("view").get_property("paintable")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self._apply()
        self.pipeline.set_state(Gst.State.PLAYING)
        self._meter_timer = GLib.timeout_add(250, self._meter)

    def stop(self, keep_effects=False):
        if self._meter_timer:
            GLib.source_remove(self._meter_timer)
            self._meter_timer = None
        if self.pipeline:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self._effects is not None and not keep_effects:
            self._effects.stop()
            self._effects = None

    # ---- effects ---------------------------------------------------------------

    def set_effects(self, blur=None, smooth=None, light=None):
        """Each 0..1. Turning the first one on (or the last one off) restarts
        the camera on the other pipeline, which takes a moment."""
        for key, value in (("blur", blur), ("smooth", smooth), ("light", light)):
            if value is not None:
                self._effect_values[key] = max(0.0, min(1.0, float(value)))
        self._update_effects()
        if self.pipeline and self._want_effects() != self._effects_on:
            self.stop(keep_effects=True)
            self.start()
            if self.on_paintable:
                self.on_paintable(self.paintable)

    def _want_effects(self):
        from . import camera_fx
        return camera_fx.available() and any(v > 0.01 for v in self._effect_values.values())

    def _update_effects(self):
        if self._effects is not None:
            v = self._effect_values
            self._effects.blur, self._effects.smooth, self._effects.light = v["blur"], v["smooth"], v["light"]

    def _on_fx_sample(self, sink):
        """A frame for the effects (on GStreamer's thread)."""
        import numpy as np
        sample = sink.emit("pull-sample")
        if sample is None or self._effects is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            raw = np.frombuffer(info.data, np.uint8)
            stride = raw.size // height
            frame = raw.reshape(height, stride)[:, :width * 4].reshape(height, width, 4).copy()
        finally:
            buf.unmap(info)
        out = self._effects.process(frame)
        data = GLib.Bytes.new(out.tobytes())
        with self._pending_lock:
            self._pending = (width, height, data)
            schedule = not self._show_scheduled
            self._show_scheduled = True
        if schedule:
            GLib.idle_add(self._show_pending)
        return Gst.FlowReturn.OK

    def _show_pending(self):
        with self._pending_lock:
            pending, self._pending = self._pending, None
            self._show_scheduled = False
        if pending is None or not isinstance(self.paintable, FrameStream):
            return False
        width, height, data = pending
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                        data, width * 4)
        self.paintable.set_texture(texture)
        return False

    def _on_error(self, _bus, msg):
        err, _dbg = msg.parse_error()
        if self.on_error:
            self.on_error(err.message)

    # ---- settings ----------------------------------------------------------

    def set_look(self, name):
        """Turns on a preset's filters (the sliders are set separately)."""
        p = PRESETS[OLD_LOOKS.get(name, name) if OLD_LOOKS.get(name, name) in PRESETS else "natural"]
        self._auto_light, self._skin, self._denoise, self._soft = (
            p["auto"], p["skin"], p["denoise"], p["soft"])
        if not self._auto_light:
            self._auto_gain = 0.0
        self._apply()

    def set_adjustments(self, brightness=None, contrast=None, saturation=None):
        """brightness -50..50, contrast 0.5..2, saturation 0..2 (1 = unchanged)."""
        for key, value in (("brightness", brightness), ("contrast", contrast),
                           ("saturation", saturation)):
            if value is not None:
                self._manual[key] = float(value)
        self._apply()

    def set_mirror(self, mirror):
        self._mirror = bool(mirror)
        self._apply()

    def _apply(self):
        if not self._fx:
            return
        fx = self._fx
        mirror = (GstVideo.VideoOrientationMethod.HORIZ if self._mirror
                  else GstVideo.VideoOrientationMethod.IDENTITY)
        if self._flip is not None:
            # Software filters (no VA-API): videobalance uses -1..1 brightness
            # and 0..2 contrast; skin tone, noise and soft focus aren't there.
            fx.set_property("brightness", max(-1.0, min(1.0, (self._manual["brightness"] + self._auto_gain) / 100)))
            contrast = self._manual["contrast"] * (1.0 + self._auto_gain / 120.0)
            fx.set_property("contrast", max(0.0, min(2.0, contrast)))
            fx.set_property("saturation", max(0.0, min(2.0, self._manual["saturation"])))
            self._flip.set_property("video-direction", mirror)
            return
        fx.set_property("brightness", max(-100.0, min(100.0, self._manual["brightness"] + self._auto_gain)))
        contrast = self._manual["contrast"] * (1.0 + self._auto_gain / 120.0)
        fx.set_property("contrast", max(0.0, min(10.0, contrast)))
        fx.set_property("saturation", self._manual["saturation"])
        fx.set_property("skin-tone", float(self._skin))
        fx.set_property("denoise", float(self._denoise))
        fx.set_property("sharpen", SHARPEN_NEUTRAL * (1.0 - self._soft))
        fx.set_property("video-direction",
                        GstVideo.VideoOrientationMethod.HORIZ if self._mirror
                        else GstVideo.VideoOrientationMethod.IDENTITY)

    def _meter(self):
        """Auto light: nudge brightness toward a comfortable level."""
        if not self.pipeline:
            return False
        if not self._auto_light:
            return True
        sample = self.pipeline.get_by_name("meter").emit("try-pull-sample", 0)
        if sample is None:
            return True
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return True
        data = info.data
        mean = sum(data) / max(1, len(data))
        buf.unmap(info)
        # mean is measured after the current correction, so step gently.
        target = 128.0
        step = (target - mean) * 0.08
        self._auto_gain = max(0.0, min(45.0, self._auto_gain + step))
        self._apply()
        return True
