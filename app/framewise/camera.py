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
from gi.repository import Gdk, GLib, Gst, GstVideo  # noqa: E402

from . import gpu, video_sink, webcam_controls  # noqa: E402
from .video_sink import FrameStream, frame_array as _frame_array  # noqa: E402,F401

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
FX_WIDTH, FX_HEIGHT = 1280, 720

# Photos: the camera opens at its biggest size for one picture. Right after
# opening it is still setting its exposure, so the first frames are skipped.
PHOTO_SETTLE_S = 0.8
PHOTO_TIMEOUT_S = 8

# Lock focus: autofocus runs this long after the camera opens (to find your
# face), then the camera holds that focus.
FOCUS_SETTLE_MS = 2500


class Camera:
    """Runs the webcam pipeline and exposes a GDK paintable to show it."""

    WIDTH, HEIGHT, FPS = 1280, 720, 30

    def __init__(self, device_id=None, name=None):
        """device_id: the chosen camera (/dev/videoN), name: its name (found by
        name if its address changed); falls back to the first one."""
        from .devices import find_camera
        cam = find_camera(device_id, name)
        self.device = cam.id if cam else "/dev/video0"
        self.mjpeg = cam.mjpeg if cam else True
        self.photo = cam.photo if cam else None    # (format, width, height) for photos
        self._still = None            # the one-picture pipeline while a photo is taken
        self._photo_job = None
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
        self._focus_lock = False
        self._focus_timer = None
        self._focus_changed = False   # autofocus turned off by us: turned back on at stop

    # Tests set this to a GStreamer source (e.g. a still picture) so they never
    # turn on the real webcam. It must end with "! ".
    test_source = None

    def _source(self, jpeg, width, height, fps=None):
        if self.test_source:
            return self.test_source
        rate = f",framerate={fps}/1" if fps else ""
        if jpeg:
            # Most webcams send 720p only as JPEG; decode it on the GPU when we can.
            return (f"v4l2src device={self.device} ! image/jpeg,width={width},height={height}{rate} ! "
                    f"{gpu.jpeg_decoder()} ! ")
        return (f"v4l2src device={self.device} ! video/x-raw,width={width},height={height}{rate} ! "
                f"videoconvert ! ")

    def _filters(self):
        if gpu.gst_va():
            return "vapostproc name=fx disable-passthrough=true ! "
        # Software: brightness, contrast and colour, and the mirror.
        return ("videoconvert ! videoflip name=flip ! videoscale ! videoconvert ! "
                "videobalance name=fx ! videoconvert ! ")

    def start(self):
        if self.test_source or self.mjpeg:
            source = self._source(True, self.WIDTH, self.HEIGHT, self.FPS)
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
            view = video_sink.sink_desc("view", sync=False)
        filters = self._filters()
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
            self.paintable = video_sink.paintable(self.pipeline, "view")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self._apply()
        self.pipeline.set_state(Gst.State.PLAYING)
        self._meter_timer = GLib.timeout_add(250, self._meter)
        if self.has_autofocus():
            if not self._focus_lock:
                # Also undoes a lock left behind if Framewise was stopped
                # without closing the camera (e.g. logging out).
                self._restore_autofocus(force=True)
            elif not self._focus_changed:
                self._lock_after_settling()
            else:
                self._set_autofocus(False)  # restarted for effects: keep the held focus

    def stop(self, keep_effects=False):
        self._end_photo()
        if self._meter_timer:
            GLib.source_remove(self._meter_timer)
            self._meter_timer = None
        self._cancel_focus_timer()
        if not keep_effects:
            self._restore_autofocus()
        if self.pipeline:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self._effects is not None and not keep_effects:
            self._effects.stop()
            self._effects = None

    # ---- photos ----------------------------------------------------------------

    def can_take_big_photo(self):
        """The camera sends bigger pictures than the bubble shows."""
        return (self.pipeline is not None and self._still is None and self.photo is not None
                and self.photo[1] * self.photo[2] > self.WIDTH * self.HEIGHT)

    def take_photo(self, frozen, on_done):
        """A photo at the camera's biggest size, with the same look (filter,
        mirror, effects). The bubble shows frozen (a texture) meanwhile: the
        camera reopens at that size for one picture, about a second.
        on_done(texture or None) runs on the main loop; the live picture is
        back by then. None: no photo (use the bubble's picture instead)."""
        if not self.can_take_big_photo():
            GLib.idle_add(lambda: on_done(None) and False)
            return
        fmt, width, height = self.photo
        effects = self._effects if self._effects_on else None
        self.stop(keep_effects=True)
        self.paintable = FrameStream()
        if frozen is not None:
            self.paintable.set_texture(frozen)
        if self.on_paintable:
            self.on_paintable(self.paintable)
        job = {"on_done": on_done, "first": None, "taken": False, "effects": effects}
        self._photo_job = job
        desc = (self._source(fmt == "jpeg", width, height) + self._filters() +
                f"video/x-raw,format=BGRA,width={width},height={height} ! "
                "appsink name=still emit-signals=true max-buffers=1 drop=true sync=false")
        try:
            self._still = Gst.parse_launch(desc)
        except GLib.Error:
            self._finish_photo(job, None)
            return
        self._fx = self._still.get_by_name("fx")
        self._flip = self._still.get_by_name("flip")
        self._apply()
        bus = self._still.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda *_: self._finish_photo(job, None))
        self._still.get_by_name("still").connect("new-sample", self._on_still_sample, job)
        def too_long():
            job["timer"] = None       # this timer is over: nothing to remove
            return self._finish_photo(job, None)
        job["timer"] = GLib.timeout_add_seconds(PHOTO_TIMEOUT_S, too_long)
        self._still.set_state(Gst.State.PLAYING)

    def _on_still_sample(self, sink, job):
        """On GStreamer's thread: skips frames while the camera settles, then
        keeps one and works on it in a thread of its own."""
        import time
        sample = sink.emit("pull-sample")
        if sample is None or job["taken"]:
            return Gst.FlowReturn.OK
        now = time.monotonic()
        if job["first"] is None:
            job["first"] = now
        if now - job["first"] < PHOTO_SETTLE_S and not self.test_source:
            return Gst.FlowReturn.OK
        job["taken"] = True
        frame = _frame_array(sample)
        if frame is None:
            GLib.idle_add(lambda: self._finish_photo(job, None) and False)
            return Gst.FlowReturn.OK

        def work():
            out = frame
            if job["effects"] is not None:
                try:
                    out = job["effects"].still(frame)
                except Exception:   # effects are a nicety: the photo still counts
                    out = frame
            h, w = out.shape[:2]
            texture = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                            GLib.Bytes.new(out.tobytes()), w * 4)
            GLib.idle_add(lambda: self._finish_photo(job, texture) and False)
        threading.Thread(target=work, daemon=True).start()
        return Gst.FlowReturn.OK

    def _end_photo(self):
        """Closes the one-picture pipeline, if any. Returns its job."""
        job, self._photo_job = self._photo_job, None
        if job is not None and job.get("timer"):
            GLib.source_remove(job["timer"])
            job["timer"] = None
        if self._still is not None:
            self._still.get_bus().remove_signal_watch()
            self._still.set_state(Gst.State.NULL)
            self._still = None
        return job

    def _finish_photo(self, job, texture):
        """Back to the live picture, then the photo goes to on_done."""
        if job is not self._photo_job:
            return False            # already finished, or the camera was closed
        self._end_photo()
        try:
            self.start()
        except GLib.Error as e:
            if self.on_error:
                self.on_error(e.message)
        if self.on_paintable and self.paintable is not None:
            self.on_paintable(self.paintable)
        job["on_done"](texture)
        return False

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
        sample = sink.emit("pull-sample")
        if sample is None or self._effects is None:
            return Gst.FlowReturn.OK
        frame = _frame_array(sample)
        if frame is None:
            return Gst.FlowReturn.OK
        height, width = frame.shape[:2]
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

    # ---- focus -------------------------------------------------------------

    def has_autofocus(self):
        """The camera can autofocus (so focus can be locked)."""
        return not self.test_source and webcam_controls.has(self.device, webcam_controls.FOCUS_AUTO)

    def set_focus_lock(self, locked):
        """Locked: the camera stops refocusing and holds its focus. When the
        camera opens, autofocus first gets a moment to find you."""
        self._focus_lock = bool(locked)
        if not self.pipeline or not self.has_autofocus():
            return
        self._cancel_focus_timer()
        if self._focus_lock:
            self._set_autofocus(False)   # hold the focus it has now
        else:
            self._set_autofocus(True)

    def _lock_after_settling(self):
        self._set_autofocus(True)

        def lock():
            self._focus_timer = None
            if self._focus_lock and self.pipeline:
                self._set_autofocus(False)
            return False
        self._focus_timer = GLib.timeout_add(FOCUS_SETTLE_MS, lock)

    def _cancel_focus_timer(self):
        if self._focus_timer:
            GLib.source_remove(self._focus_timer)
            self._focus_timer = None

    def _set_autofocus(self, on):
        if not on:
            self._focus_changed = True
        webcam_controls.set_value(self.device, webcam_controls.FOCUS_AUTO, 1 if on else 0)

    def _restore_autofocus(self, force=False):
        """Back to the camera's default (autofocus on for nearly all), so
        other apps aren't left with a locked focus."""
        if not (self._focus_changed or force):
            return
        self._focus_changed = False
        default = webcam_controls.default(self.device, webcam_controls.FOCUS_AUTO)
        webcam_controls.set_value(self.device, webcam_controls.FOCUS_AUTO, 1 if default is None else default)

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
