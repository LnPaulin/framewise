"""Preview playback for the editor.

Plays the original take with the edit applied live: the video is decoded
and scaled down on the GPU, cropped to the recorded area, and every sound
track goes through its own volume into one mixer. While playing, a timer
jumps over parts that were cut out and stops at the end of the trim, so
what you watch is what will be saved.
"""

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from . import edit_model as em  # noqa: E402
from . import gpu  # noqa: E402

Gst.init(None)

PREVIEW_MAX_WIDTH = 1920


class Player:
    def __init__(self, take, video_size, crop_px=None, track_names=(), mic_clean=None):
        """crop_px: (x, y, w, h) of the area in video pixels, or None."""
        self.doc = None
        self.on_position = None   # (recording_time) -> None, while playing or after seeks
        self.on_state = None      # (playing: bool) -> None
        self.track_names = list(track_names)
        self.volumes = {}
        self.playing = False
        self._timer = None

        vw, vh = video_size
        cx, cy, cw, ch = crop_px if crop_px else (0, 0, vw, vh)
        scale = min(1.0, PREVIEW_MAX_WIDTH / cw)
        pw = max(2, int(vw * scale) & ~1)
        ph = max(2, int(vh * scale) & ~1)
        crop = ""
        if crop_px:
            left, top = int(cx * scale), int(cy * scale)
            right = max(0, pw - left - int(cw * scale))
            bottom = max(0, ph - top - int(ch * scale))
            crop = f"videocrop left={left} top={top} right={right} bottom={bottom} ! "

        # Every branch is described up front (demux pads link when they
        # appear), so the mixer has all its inputs before it starts.
        desc = (f'filesrc location="{take}" ! matroskademux name=demux '
                f"demux.video_0 ! queue max-size-time=1000000000 ! h264parse ! {gpu.h264_decoder()} ! "
                f"{gpu.convert()} ! video/x-raw,format=BGRA,width={pw},height={ph} ! {crop}"
                f"gtk4paintablesink name=view ")
        if self.track_names:
            desc += "audiomixer name=mix ! audioconvert ! audioresample ! autoaudiosink "
            for i, track in enumerate(self.track_names):
                desc += (f"demux.audio_{i} ! queue max-size-time=1000000000 ! decodebin3 ! "
                         f"audioconvert ! audioresample ! volume name=vol_{track} ! mix. ")
            if mic_clean:
                # The noise-cleaned microphone, played instead of the raw one when on.
                desc += (f'filesrc location="{mic_clean}" ! decodebin3 ! audioconvert ! '
                         f"audioresample ! volume name=vol_mic_clean mute=true ! mix. ")
        self.pipeline = Gst.parse_launch(desc)
        self.paintable = self.pipeline.get_by_name("view").get_property("paintable")
        for track in self.track_names:
            self.volumes[track] = self.pipeline.get_by_name(f"vol_{track}")
        self.clean_volume = self.pipeline.get_by_name("vol_mic_clean")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.pipeline.set_state(Gst.State.PAUSED)

    # ---- wiring --------------------------------------------------------------

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self.pause()
        elif msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            print("preview error:", err.message, flush=True)

    # ---- edit ----------------------------------------------------------------

    def set_document(self, doc):
        self.doc = doc
        self._apply_volumes()

    def _apply_volumes(self):
        if not self.doc:
            return
        clean_on = bool(self.clean_volume and (self.doc.get("denoise") or {}).get("mic"))
        for track, vol in self.volumes.items():
            vol.set_property("volume", float(self.doc["gain"].get(track, 1.0)))
            vol.set_property("mute", bool(self.doc["muted"].get(track, False))
                             or (track == "mic" and clean_on))
        if self.clean_volume:
            self.clean_volume.set_property("volume", float(self.doc["gain"].get("mic", 1.0)))
            self.clean_volume.set_property("mute", not clean_on or bool(self.doc["muted"].get("mic")))
        # Sound sync: the system sound moved later (+) or earlier (-).
        vol = self.volumes.get("system")
        if vol is not None:
            pad = vol.get_static_pad("src")
            offset = int(float((self.doc.get("sync") or {}).get("system", 0.0)) * Gst.SECOND)
            if pad.get_offset() != offset:
                pad.set_offset(offset)
                # Flush so the change is heard at once.
                self.seek(self.position())

    # ---- transport -------------------------------------------------------------

    def position(self):
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok and pos >= 0 else 0.0

    def seek(self, t):
        self.pipeline.seek_simple(Gst.Format.TIME,
                                  Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                  int(max(0.0, t) * Gst.SECOND))
        if self.on_position:
            self.on_position(t)

    def play(self):
        if self.doc:
            t = self.position()
            end = self.doc["trim"][1]
            if t >= end - 0.05 or t < self.doc["trim"][0]:
                self.seek(self.doc["trim"][0])
            else:
                snapped = em.snap_to_kept(self.doc, t)
                if abs(snapped - t) > 0.01:
                    self.seek(snapped)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.playing = True
        if not self._timer:
            self._timer = GLib.timeout_add(20, self._tick)
        if self.on_state:
            self.on_state(True)

    def pause(self):
        self.pipeline.set_state(Gst.State.PAUSED)
        self.playing = False
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        if self.on_state:
            self.on_state(False)

    def toggle(self):
        self.pause() if self.playing else self.play()

    def _tick(self):
        t = self.position()
        if self.doc:
            if t >= self.doc["trim"][1] - 0.01:
                self.pause()
                self.seek(self.doc["trim"][1])
                return False
            i = em.cut_at(self.doc, t)
            if i is not None:
                # Jump over the cut, so the preview plays exactly what is kept.
                self.seek(self.doc["cuts"][i][1])
                return True
        if self.on_position:
            self.on_position(t)
        return True

    def close(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        self.pipeline.get_bus().remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
