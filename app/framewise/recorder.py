"""The recording pipeline: screen + optional system sound + optional microphone.

Everything goes into one crash-safe MKV "take":
  video   H.264 from the Intel encoder, constant frame rate
  audio 1 system sound (FLAC, lossless), if on
  audio 2 microphone (FLAC, lossless), if on
Each sound source keeps its own track so the editor can adjust them separately.

Pausing never stops the capture. While paused, buffers are dropped in every
branch (video before its encoder, sound after its encoder), and on resume each branch's pad offset moves back by
the paused time, so the file has no gap and sound stays in sync with video.
The recorder starts out paused: the pipeline warms up during the countdown
and the first frame is the one right after the countdown ends.
"""

import os
import shutil
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from . import gpu  # noqa: E402

Gst.init(None)

# Constant-quantizer settings for the Intel encoder (lower = better).
# File size doesn't matter to this user, so even "balanced" is generous.
QUALITY_QP = {
    "max": (12, 14),
    "high": (16, 18),
    "balanced": (20, 22),
    "small": (25, 27),
}

MIN_FREE_TO_START = 2_000_000_000
MIN_FREE_TO_CONTINUE = 500_000_000


class RecorderError(Exception):
    pass


def free_bytes(path):
    return shutil.disk_usage(path).free


class Recorder:
    def __init__(self, capture, out_path, *, fps=60, quality="high",
                 system_audio=True, microphone=True, microphone_device="",
                 window_mode=False, window_size=None):
        self.capture = capture
        self.out_path = str(out_path)
        self.fps = fps
        self.quality = quality
        self.system_audio = system_audio
        self.microphone = microphone
        self.microphone_device = microphone_device
        self.window_mode = window_mode
        self.window_size = window_size   # (w, h) of a real frame of the window, if known

        self.on_error = None      # (message) -> None
        self.on_finished = None   # (ok: bool) -> None
        self.on_low_disk = None   # () -> None
        self.on_mic_lost = None   # () -> None

        self._pipeline = None
        self._entry_pads = []
        self._lock = threading.Lock()
        self._paused = True
        self._offset = 0              # total paused running time, ns
        self._paused_at = 0           # running time the current pause began, or None
        self._resumed_at = None       # running time of the latest resume
        self._recorded_before = 0.0   # seconds recorded before the current run
        self._run_started = None      # monotonic time the current run began
        # Each stretch actually recorded: (monotonic start, monotonic end,
        # seconds already in the video when it began). Maps outside events,
        # like pointer samples, onto the recording's own clock.
        self.runs = []
        self._stopping = False
        self._screen_ended = False
        self._disk_timer = None
        self._eos_timer = None
        self.has_video = False

    # ---- building -------------------------------------------------------

    def _video_branch(self):
        s = self.capture.stream
        qpi, qpp = QUALITY_QP.get(self.quality, QUALITY_QP["high"])
        size = ""
        par = ""
        if self.window_mode:
            # A window can be resized while recording; keep the video size fixed,
            # at the size of the window's real picture. Square pixels: a picture
            # that no longer fits gets black bars instead of being stretched.
            # (Screens keep their own size, so they don't need this; with GNOME's
            # screen feed it even made the converter fail to start.)
            w, h = self.window_size or (s.width, s.height)
            size = f",width={w - w % 2},height={h - h % 2}"
            par = ",pixel-aspect-ratio=1/1"
        # On the graphics chip when it can (VA-API), in software otherwise.
        if gpu.gst_va():
            convert, memory = "vapostproc add-borders=true", "video/x-raw(memory:VAMemory)"
        else:
            convert, memory = "videoconvert ! videoscale add-borders=true", "video/x-raw"
        return (
            f"pipewiresrc name=vsrc fd={self.capture.fd} path={self.capture.stream.node_id} "
            f"do-timestamp=true keepalive-time=100 on-disconnect=eos client-name=framewise ! "
            f"queue max-size-buffers=8 leaky=downstream ! "
            f"{convert} ! "
            f"{memory},format=NV12{size}{par} ! "
            f"videorate ! capsfilter name=vgate caps=\"{memory},framerate={self.fps}/1\" ! "
            f"queue max-size-buffers=30 ! "
            f"{gpu.h264_encoder((qpi, qpp), self.fps * 2)} ! "
            f"h264parse ! queue max-size-time=2000000000 ! mux. "
        )

    @staticmethod
    def _audio_branch(name, device):
        # The device is set as a property after parsing (see _build), so a
        # device name can never change the pipeline.
        return (
            f"pulsesrc name={name} client-name=framewise-{name} ! "
            f"audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! "
            f"queue max-size-time=2000000000 ! flacenc name={name}enc ! "
            f"queue max-size-time=2000000000 ! mux. "
        )

    def _build(self):
        desc = (f"matroskamux name=mux writing-app=framewise ! "
                f"filesink name=fsink location=\"{self.out_path}\" ")
        desc += self._video_branch()
        if self.system_audio:
            desc += self._audio_branch("system", "@DEFAULT_MONITOR@")
        if self.microphone:
            desc += self._audio_branch("mic", self.microphone_device)
        pipe = Gst.parse_launch(desc)
        self.system_delay = 0.0
        if self.system_audio:
            source = pipe.get_by_name("system")
            source.set_property("device", "@DEFAULT_MONITOR@")
            # The system sound is captured as it leaves the computer, but heard
            # later (Bluetooth headphones: about 0.2 s). Delay it by that much,
            # so it lines up with a voice singing or talking along to it.
            from .devices import output_delay
            self.system_delay = output_delay()
            if self.system_delay > 0:
                source.get_static_pad("src").set_offset(int(self.system_delay * Gst.SECOND))
        if self.microphone and self.microphone_device:
            pipe.get_by_name("mic").set_property("device", self.microphone_device)
        # One clock for every branch keeps sound and picture aligned.
        pipe.use_clock(Gst.SystemClock.obtain())

        # Video is gated after the frame-rate step and before the encoder
        # (H.264 frames depend on each other), sound after its encoder (each FLAC frame stands alone, and the encoder
        # mustn't see the stream jump).
        names = ["vgate"] + (["systemenc"] if self.system_audio else []) + (["micenc"] if self.microphone else [])
        for name in names:
            pad = pipe.get_by_name(name).get_static_pad("src")
            pad.add_probe(Gst.PadProbeType.BUFFER, self._gate, name)
            self._entry_pads.append(pad)
            # A flush means "start over" to the file writer: it empties the file.
            # The screen feed can send one (e.g. as GNOME ends the share), which
            # would wipe the whole recording. Nothing downstream ever needs one.
            pad.add_probe(Gst.PadProbeType.EVENT_FLUSH, lambda *_: Gst.PadProbeReturn.DROP)
        pipe.get_by_name("fsink").get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_FLUSH, lambda *_: Gst.PadProbeReturn.DROP)
        # GNOME's own "stop sharing" button (top right) cuts the screen share.
        # The video branch then ends on its own; finish the whole file from it.
        pipe.get_by_name("vgate").get_static_pad("src").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._watch_video_end)

        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        return pipe

    def _watch_video_end(self, _pad, info):
        if info.get_event().type == Gst.EventType.EOS and not self._stopping:
            GLib.idle_add(self._screen_gone)
        return Gst.PadProbeReturn.OK

    def _screen_gone(self):
        """The screen share ended outside the app: stop and keep what was recorded."""
        self.mark_screen_ended()
        self.stop()
        return False

    def mark_screen_ended(self):
        """Called when the screen share was stopped outside the app (for
        example with GNOME's sharing icon). The video branch then complains
        about the missing feed; those complaints are expected, not errors."""
        self._screen_ended = True

    def _gate(self, pad, info, name):
        """Keeps a buffer only if it was captured while recording was running.

        Deciding by capture time (not arrival time) matters: buffers reach
        this point a little late, and one captured just before a resume would
        otherwise land before zero once the offset moves back. That one is
        often the video keyframe, without which the start can't be played.
        """
        buf = info.get_buffer()
        event = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
        if buf is None or event is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.DROP
        rt = event.parse_segment().to_running_time(Gst.Format.TIME, buf.pts)
        with self._lock:
            if self._resumed_at is None or rt < self._resumed_at:
                return Gst.PadProbeReturn.DROP
            if self._paused_at is not None and rt >= self._paused_at:
                return Gst.PadProbeReturn.DROP
        if name == "vgate":
            self.has_video = True
        return Gst.PadProbeReturn.OK

    # ---- control ----------------------------------------------------------

    def prepare(self):
        """Builds and starts the pipeline, still paused (nothing is written yet)."""
        if free_bytes(GLib.path_get_dirname(self.out_path)) < MIN_FREE_TO_START:
            raise RecorderError("Not enough space to record. Free up at least 2 GB and try again.")
        self._pipeline = self._build()
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RecorderError("The recording could not start.")
        self._disk_timer = GLib.timeout_add_seconds(5, self._check_disk)

    def _running_time(self):
        clock = self._pipeline.get_clock()
        if clock is None:
            return 0
        return max(0, clock.get_time() - self._pipeline.get_base_time())

    def resume(self):
        with self._lock:
            if not self._paused or self._stopping:
                return
            now = self._running_time()
            self._offset += now - self._paused_at
            for pad in self._entry_pads:
                pad.set_offset(-self._offset)
            self._resumed_at = now
            self._paused_at = None
            self._paused = False
            self._run_started = time.monotonic()
            self.runs.append([self._run_started, None, self._recorded_before])

    def pause(self):
        with self._lock:
            if self._paused or self._stopping:
                return
            self._paused = True
            self._paused_at = self._running_time()
            ended = time.monotonic()
            if self.runs:
                self.runs[-1][1] = ended
            self._recorded_before += ended - self._run_started
            self._run_started = None

    @property
    def paused(self):
        return self._paused

    def elapsed(self):
        """Seconds in the finished video so far."""
        extra = time.monotonic() - self._run_started if self._run_started else 0
        return self._recorded_before + extra

    def stop(self):
        """Closes the file properly. on_finished is called once it is written."""
        if self._stopping or self._pipeline is None:
            return
        self.pause()
        with self._lock:
            self._stopping = True
        if self._disk_timer:
            GLib.source_remove(self._disk_timer)
            self._disk_timer = None
        self._pipeline.send_event(Gst.Event.new_eos())
        # If the end-of-stream never comes back, close anyway.
        self._eos_timer = GLib.timeout_add_seconds(8, self._finish, False)

    def _finish(self, ok):
        if self._eos_timer:
            GLib.source_remove(self._eos_timer)
            self._eos_timer = None
        if self._pipeline is not None:
            self._pipeline.get_bus().remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self.capture.close()
        if self.on_finished:
            # Even after an error the MKV holds everything written so far, and
            # ffmpeg can read it, so a take with video is always worth saving.
            try:
                size = os.path.getsize(self.out_path)
            except OSError:
                size = 0
            self.on_finished(self.has_video and size > 0)
        return False

    # ---- events -------------------------------------------------------------

    def _check_disk(self):
        try:
            low = free_bytes(GLib.path_get_dirname(self.out_path)) < MIN_FREE_TO_CONTINUE
        except OSError:
            low = False
        if low:
            self._disk_timer = None
            if self.on_low_disk:
                self.on_low_disk()
            self.stop()
            return False
        return True

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self._finish(True)
        elif msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            src = msg.src.get_name() if msg.src else ""
            if src.startswith("mic") and not self._stopping:
                # Losing the microphone shouldn't lose the recording. End just
                # that track so the muxer doesn't wait on it forever.
                mic = self._pipeline.get_by_name("mic")
                peer = mic.get_static_pad("src").get_peer() if mic else None
                if peer:
                    peer.send_event(Gst.Event.new_eos())
                if self.on_mic_lost:
                    self.on_mic_lost()
                return
            if src == "vsrc":
                # The screen share went away (older PipeWire reports this as an
                # error rather than an end). Treat it as a normal stop.
                self._screen_ended = True
            if self._screen_ended:
                # Fallout from the feed being cut (e.g. "Failed to push one
                # frame" from the encoder). The file is fine; finish quietly.
                if self._stopping:
                    self._finish(True)
                else:
                    self.stop()
                return
            if self.on_error:
                self.on_error(f"{err.message} ({src})")
            if self._stopping:
                self._finish(False)
            else:
                self.stop()


def to_recording_time(runs, mono):
    """A monotonic time (seconds) -> seconds into the recording, or None if paused."""
    for start, end, offset in runs:
        if start <= mono and (end is None or mono < end):
            return offset + (mono - start)
    return None
