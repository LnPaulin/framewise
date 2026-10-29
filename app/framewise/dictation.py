"""Dictation: speak, and the words are typed where the cursor is.

Press the shortcut (or choose Dictate in the menu): a "Listening" pill shows
at the top of the screen and the microphone is recorded. It finishes when
the shortcut is pressed again, the pill is clicked, or after a short pause
once something was said. The speech model (the same one the editor uses,
run on this computer) turns the sound into text, and GNOME Shell pastes it
into the window that has the focus. What was on the clipboard before stays
in the clipboard history.

The model is kept loaded for a while after each use, so the next dictation
answers quickly; it is unloaded after IDLE_UNLOAD seconds to free memory.
"""

import json
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

from . import speech  # noqa: E402
from .state import settings  # noqa: E402

Gst.init(None)

SPEECH_DB = -42.0       # louder than this (RMS) counts as speaking
SPEECH_FRAMES = 3       # ... for this many 0.1 s steps in a row
PAUSE_TO_FINISH = 2.5   # seconds of quiet after speaking that finish
NOTHING_HEARD = 12.0    # seconds without speech before giving up
MAX_LENGTH = 300.0      # seconds; longer dictations finish on their own
IDLE_UNLOAD = 600       # seconds the model stays loaded after the last use

# Whisper sometimes "hears" these in silence or noise.
PHANTOMS = {"thank you.", "thanks for watching!", "thank you for watching!", "you", "bye.",
            "merci.", "sous-titrage st' 501", "sous-titres réalisés par la communauté d'amara.org"}


def clean(text):
    """The text to type, or "" when nothing real was said."""
    text = " ".join((text or "").split())
    return "" if text.lower() in PHANTOMS else text


class SpeechServer:
    """The speech model in its own process, kept loaded between dictations."""

    def __init__(self):
        self._proc = None
        self._stdin = None
        self._ready = False
        self._waiting = []      # requests sent before the model was ready
        self._pending = []      # callbacks for answers, in order
        self._idle_id = 0

    def warm_up(self):
        """Starts loading the model now (while the person speaks)."""
        if self._proc is not None:
            self._touch()
            return
        try:
            self._proc = Gio.Subprocess.new(
                [str(speech.PYTHON), str(speech.WORKER), "serve", str(speech.MODEL_DIR)],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
                | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as e:
            print("dictation: speech model didn't start:", e.message, flush=True)
            self._proc = None
            return
        self._ready = False
        self._stdin = Gio.DataOutputStream.new(self._proc.get_stdin_pipe())
        stream = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        proc = self._proc
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._line, proc)
        proc.wait_async(None, self._ended)
        self._touch()

    def transcribe(self, wav, language, on_done):
        """on_done(text or None, error) on the main loop."""
        self.warm_up()
        if self._proc is None:
            on_done(None, "The speech model couldn't start")
            return
        request = json.dumps({"wav": str(wav), "language": language or ""}) + "\n"
        if self._ready:
            self._send(request, on_done)
        else:
            self._waiting.append((request, on_done))

    def _send(self, request, on_done):
        try:
            self._stdin.put_string(request, None)
            self._stdin.flush(None)
        except GLib.Error as e:
            on_done(None, e.message)
            return
        self._pending.append(on_done)
        self._touch()

    def _line(self, stream, result, proc):
        try:
            text, _n = stream.read_line_finish_utf8(result)
        except GLib.Error:
            text = None
        if text is None or proc is not self._proc:
            return
        try:
            msg = json.loads(text)
        except ValueError:
            msg = {}
        if msg.get("ready"):
            self._ready = True
            waiting, self._waiting = self._waiting, []
            for request, on_done in waiting:
                self._send(request, on_done)
        elif ("text" in msg or "error" in msg) and self._pending:
            self._pending.pop(0)(msg.get("text"), msg.get("error", ""))
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._line, proc)

    def _ended(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is not self._proc:
            return
        self._proc = None
        self._ready = False
        failed = self._pending + [cb for _r, cb in self._waiting]
        self._pending, self._waiting = [], []
        for on_done in failed:
            on_done(None, "The speech model stopped")

    def _touch(self):
        if self._idle_id:
            GLib.source_remove(self._idle_id)
        self._idle_id = GLib.timeout_add_seconds(IDLE_UNLOAD, self._unload)

    def _unload(self):
        self._idle_id = 0
        if self._pending or self._waiting:
            self._touch()
            return False
        self.stop()
        return False

    def stop(self):
        if self._idle_id:
            GLib.source_remove(self._idle_id)
            self._idle_id = 0
        proc, self._proc = self._proc, None
        self._ready = False
        if proc is not None:
            proc.force_exit()


class Dictation:
    IDLE, LISTENING, WRITING = "idle", "listening", "writing"
    # Where the sound comes from (tests put a sound file here instead).
    SOURCE = "pulsesrc name=src client-name=framewise-dictation"

    def __init__(self, app):
        self.app = app
        self.shell = app.session.shell
        self.shell.on_dictation_clicked = self.finish
        self.server = SpeechServer()
        self.state = self.IDLE
        self._pipe = None
        self._wav = None
        self._started = 0.0
        self._loud_run = 0
        self._heard = False
        self._last_loud = 0.0
        self._timer = 0

    # ---- start and finish ---------------------------------------------------------

    def toggle(self):
        if self.state == self.IDLE:
            self.start()
        elif self.state == self.LISTENING:
            self.finish()
        # While writing, a press waits for the text.

    def start(self):
        if not speech.is_ready():
            self._needs_model()
            return
        self._wav = Path(tempfile.mkstemp(prefix="framewise-dictation-", suffix=".wav")[1])
        try:
            self._pipe = Gst.parse_launch(
                f"{self.SOURCE} ! audioconvert ! audioresample ! "
                "audio/x-raw,format=S16LE,rate=16000,channels=1 ! "
                "level name=level interval=100000000 post-messages=true ! "
                f'wavenc ! filesink location="{self._wav}"')
        except GLib.Error as e:
            self._failed(f"The microphone couldn't be opened: {e.message}")
            return
        device = settings.get("microphone_device") or ""
        if device and self._pipe.get_by_name("src") is not None:
            self._pipe.get_by_name("src").set_property("device", device)
        bus = self._pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        if self._pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._failed("The microphone couldn't be opened")
            return
        self.app.hold()
        self.state = self.LISTENING
        self._started = self._last_loud = time.monotonic()
        self._loud_run, self._heard = 0, False
        self._timer = GLib.timeout_add(200, self._check_time)
        self.server.warm_up()           # the model loads while the person speaks
        self._show("listening")

    def finish(self):
        """Stops listening; the text is typed once the sound is written."""
        if self.state != self.LISTENING:
            return
        self.state = self.WRITING
        self._stop_timer()
        self._show("writing")
        self._pipe.send_event(Gst.Event.new_eos())

    def cancel(self, message=None):
        self._close_pipe()
        self._stop_timer()
        if self._wav:
            self._wav.unlink(missing_ok=True)
            self._wav = None
        was_active = self.state != self.IDLE
        self.state = self.IDLE
        self._hide()
        if was_active:
            self.app.release()
        if message:
            self.app.notify("Dictation", message)

    # ---- listening -----------------------------------------------------------------

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.ELEMENT and msg.get_structure().get_name() == "level":
            self._on_level(msg.get_structure())
        elif msg.type == Gst.MessageType.EOS:
            self._close_pipe()
            self._transcribe()
        elif msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            self.cancel(f"The microphone stopped: {err.message}")

    def _on_level(self, s):
        try:
            rms = max(s.get_value("rms"))
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        if rms > SPEECH_DB:
            self._loud_run += 1
            if self._loud_run >= SPEECH_FRAMES:
                self._heard = True
            self._last_loud = now
        else:
            self._loud_run = 0

    def _check_time(self):
        if self.state != self.LISTENING:
            self._timer = 0
            return False
        now = time.monotonic()
        if self._heard and now - self._last_loud >= PAUSE_TO_FINISH:
            self._timer = 0
            self.finish()
            return False
        if not self._heard and now - self._started >= NOTHING_HEARD:
            self._timer = 0
            self.cancel("Nothing was heard. Check the microphone in Settings → Sound.")
            return False
        if now - self._started >= MAX_LENGTH:
            self._timer = 0
            self.finish()
            return False
        return True

    # ---- writing -------------------------------------------------------------------

    def _transcribe(self):
        wav = self._wav
        if wav is None:
            return
        language = settings.get("dictation_language") or ""

        def done(text, error):
            wav.unlink(missing_ok=True)
            if self._wav is wav:
                self._wav = None
            if self.state != self.WRITING:
                return
            text = clean(text)
            if error:
                self.cancel(f"Speech recognition failed: {error}")
            elif not text:
                self.cancel("Nothing was recognized. Try speaking a little louder or closer.")
            else:
                self._type(text)

        self.server.transcribe(wav, language, done)

    def _type(self, text):
        typed = self.shell.has_dictation and self.shell.type_text(text + " ")
        if not typed:
            from . import wlcopy
            if wlcopy.copy(text.encode(), "text/plain;charset=utf-8"):
                self.app.notify("Dictation copied", f"Paste it with Ctrl+V: {text[:120]}")
            else:
                self.app.notify("Dictation", text[:200])
        self.cancel()

    # ---- helpers -------------------------------------------------------------------

    def _needs_model(self):
        n = Gio.Notification.new("Dictation needs speech recognition")
        n.set_body(f"Download it once in Settings → Recording ({speech.DOWNLOAD_SIZE}). "
                   "It runs on this computer; nothing is uploaded.")
        n.set_default_action("app.preferences")
        n.add_button("Open Settings", "app.preferences")
        self.app.send_notification("dictation", n)

    def _failed(self, message):
        self._close_pipe()
        if self._wav:
            self._wav.unlink(missing_ok=True)
            self._wav = None
        self.app.notify("Dictation", message)

    def _show(self, state):
        if self.shell.has_dictation:
            self.shell.show_dictation(state)

    def _hide(self):
        if self.shell.has_dictation:
            self.shell.hide_dictation()

    def _stop_timer(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _close_pipe(self):
        pipe, self._pipe = self._pipe, None
        if pipe is not None:
            pipe.get_bus().remove_signal_watch()
            pipe.set_state(Gst.State.NULL)

    def shutdown(self):
        self.cancel()
        self.server.stop()
