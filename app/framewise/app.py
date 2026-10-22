"""The app: one running instance that every command and shortcut talks to.

`framewise record` toggles: it opens the chooser when idle and stops the
recording when one is running. Running it again while it's already open
reaches the same instance (GTK forwards the command line over D-Bus).
"""

import json
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import export, frame, portal, ui  # noqa: E402
from .shell import ShellCountdown, ShellHelper  # noqa: E402
from .recorder import Recorder, RecorderError  # noqa: E402
from .state import settings  # noqa: E402

APP_ID = "com.gitcotech.Framewise"

# Pixels trimmed off each edge of a whole-screen recording, where the
# extension's border is drawn (2 px border plus a little margin).
SCREEN_BORDER_TRIM = 4

USAGE = """Usage: framewise              open the Framewise window
       framewise record [--now | --stop | --pause]
       framewise screenshot [--screen] [--delay SECONDS]
       framewise text
       framewise clipboard
       framewise settings
       framewise camera
       framewise edit TAKE_FOLDER

  record          open the recorder, or stop the recording if one is running
  record --now    start right away with the last used mode and options
  record --stop   stop the current recording
  record --pause  pause or resume the current recording
  screenshot      freeze the screen and choose an area, a window or a screen
  screenshot --screen       take the whole screen right away
  screenshot --delay 3      wait 3 seconds first
  text            choose an area and copy the text in it
  clipboard       open the clipboard history
  settings        open the settings window
  camera          show or hide the camera bubble (also handy as a mirror)
  edit FOLDER     open a recording's take folder in the editor
"""


class Session:
    """One recording, from choosing what to record to the saved file."""

    IDLE, PREPARING, RECORDING, SAVING = range(4)

    def __init__(self, app):
        self.app = app
        self.phase = self.IDLE
        self.capture = None
        self.recorder = None
        self.take_dir = None
        self.crop = None
        self.pill = None
        self.tick = None
        self.overlay = None  # selector or countdown, while one is shown
        self.monitor = None
        self.shell = ShellHelper()
        self.shell.on_pause = self.toggle_pause
        self.shell.on_stop = self.stop
        self.use_shell = False

    # ---- start ----------------------------------------------------------------

    def begin(self, mode):
        self.phase = self.PREPARING
        self.app.hold()
        kind = "window" if mode == "window" else "monitor"
        # The editor draws the pointer itself (smooth, with click rings) when
        # it knows where the pointer was: then the video leaves it out.
        self.cursor = ("hidden" if kind == "monitor" and self.shell.has_drawn_pointer
                       else "embedded")
        screen = settings.get("screen")
        if not ui.monitor_by_connector(screen):
            monitors = ui.list_monitors()
            screen = monitors[0][0] if monitors else None
        portal.open_capture(
            kind, self.cursor,
            on_ready=lambda cap: self._on_capture(cap, mode),
            on_error=self._on_portal_error,
            on_closed=self._on_portal_closed,
            screen=screen,
            screen_of_stream=lambda st: ui.monitor_for(st).get_connector())

    def _on_portal_error(self, message):
        if message != "cancelled":
            self.app.notify("The screen could not be recorded", message)
        self._reset()

    def _on_capture(self, cap, mode):
        if self.phase != self.PREPARING:
            cap.close()
            return
        self.capture = cap
        self.monitor = ui.monitor_for(cap.stream)
        if mode == "area":
            frame.grab(cap, self._show_selector)
        else:
            self._prepare(window_mode=(mode == "window"))

    def _show_selector(self, still):
        if self.phase != self.PREPARING:
            return
        self.overlay = ui.AreaSelector(self.app, self.monitor, self._on_area, still)

    def _on_area(self, rect):
        self.overlay = None
        if rect is None:
            self.cancel()
            return
        self.crop = rect
        self._prepare(window_mode=False)

    def _prepare(self, window_mode):
        self.window_mode = window_mode
        self.take_dir = export.new_take_dir()
        self.recorder = Recorder(
            self.capture, self.take_dir / "take.mkv",
            fps=settings.get("fps"), quality=settings.get("quality"),
            system_audio=settings.get("system_audio"), microphone=settings.get("microphone"),
            microphone_device=settings.get("microphone_device"), window_mode=window_mode)
        self.recorder.on_finished = self._on_recorded
        self.recorder.on_error = lambda m: self.app.notify("Recording error", m)
        self.recorder.on_low_disk = lambda: self.app.notify(
            "Recording stopped", "Disk space is almost full.")
        self.recorder.on_mic_lost = lambda: self.app.notify(
            "Microphone unavailable", "Recording continues without it.")
        try:
            self.recorder.prepare()
        except RecorderError as e:
            self.app.notify("Can't record", str(e))
            self.capture.close()
            self._reset()
            return
        self.use_shell = self.shell.available
        self._show_border(window_mode)
        export.write_take_info(self.take_dir, {**self._take_info(), "crop": self.crop})
        seconds = settings.get("countdown") or 0
        # The camera bubble appears now, so it is ready (and recorded) from the
        # first frame. A window recording doesn't show it, so it isn't opened.
        camera_wait = 0
        if settings.get("camera") and not window_mode and not self.app.camera_open:
            self.app.show_camera(for_recording=True)
            camera_wait = 2500 if seconds == 0 else 0
        if seconds > 0:
            if self.use_shell and self.shell.has_countdown:
                self.overlay = ShellCountdown(self.shell, self.monitor, seconds, self._on_countdown)
            else:
                self.overlay = ui.Countdown(self.app, self.monitor, seconds, self._on_countdown)
        elif camera_wait:
            # No countdown: start once the camera shows a picture (or after 2.5 s).
            self.app.bubble.when_ready(lambda: self._on_countdown(True), camera_wait)
        else:
            # Let the chooser or selection layer disappear before the first frame.
            GLib.timeout_add(400, lambda: self._on_countdown(True) and False)

    def _take_info(self):
        return {"fps": settings.get("fps"), "quality": settings.get("quality"),
                "system_delay": round(getattr(self.recorder, "system_delay", 0.0), 4),
                "cursor": getattr(self, "cursor", "embedded"),
                "system_audio": settings.get("system_audio"),
                "microphone": settings.get("microphone")}

    def _show_border(self, window_mode):
        """A red border marking what is recorded, drawn by the extension."""
        if not (self.use_shell and self.shell.has_borders):
            return
        g = self.monitor.get_geometry()
        if self.crop:
            fx, fy, fw, fh = self.crop
            self.shell.show_area(g.x + fx * g.width, g.y + fy * g.height,
                                 fw * g.width, fh * g.height)
        elif window_mode:
            s = self.capture.stream
            self.shell.show_window_border(s.width, s.height)
        else:
            # Whole screen: the border sits on the screen's edges and is
            # captured, so the saved video leaves out that thin strip.
            self.shell.show_screen_border(g.x, g.y, g.width, g.height)
            m = SCREEN_BORDER_TRIM
            self.crop = (m / g.width, m / g.height, 1 - 2 * m / g.width, 1 - 2 * m / g.height)

    def _on_countdown(self, ok):
        self.overlay = None
        if not ok:
            self.cancel()
            return
        # Give the countdown layer a moment to leave the screen.
        GLib.timeout_add(150, self._go)

    def _go(self):
        if self.phase != self.PREPARING:
            return False
        self.phase = self.RECORDING
        self.recorder.resume()
        # Pointer and clicks for zoom on click (screen and area recordings).
        self.tracking = (self.use_shell and not self.window_mode
                         and self.shell.has_pointer_track)
        if self.tracking:
            self.shell.start_pointer_track()
        if self.app.bubble:
            self.app.keep_camera_above()
        if self.use_shell:
            # Controls in the top bar, where no window can cover them.
            self.shell.show_controls()
        else:
            self.pill = ui.ControlsPill(self.app, self.toggle_pause, self.stop)
            self.pill.present()
        self.tick = GLib.timeout_add(250, self._update_pill)
        return False

    def _update_pill(self):
        if self.recorder:
            if self.pill:
                self.pill.update(self.recorder.elapsed(), self.recorder.paused)
            if self.use_shell:
                self.shell.update_controls(self.recorder.elapsed(), self.recorder.paused)
        return True

    # ---- control ------------------------------------------------------------

    def toggle_pause(self):
        if self.phase != self.RECORDING:
            return
        if self.recorder.paused:
            self.recorder.resume()
        else:
            self.recorder.pause()
        self._update_pill()

    def stop(self):
        if self.phase == self.RECORDING:
            self.phase = self.SAVING
            self._close_pill()
            self.recorder.stop()
            self._save_pointer_track()
            # A camera bubble the recording opened goes away with it; one opened
            # on its own (from the Camera tool, as a mirror) stays.
            if self.app.bubble and self.app.bubble_for_recording:
                self.app.bubble.close()
        elif self.phase == self.PREPARING:
            self.cancel()

    def _save_pointer_track(self):
        """Writes pointer.json next to the take: where the pointer was, on the
        recording's own clock and in fractions of the recorded picture."""
        if not getattr(self, "tracking", False):
            return
        self.tracking = False
        from .recorder import to_recording_time
        raw = self.shell.stop_pointer_track()
        if isinstance(raw, dict):
            raw, typed = raw.get("pointer", []), raw.get("typing", [])
        else:
            typed = []
        g = self.monitor.get_geometry()
        # Where text was typed (the text cursor), for zooms that follow typing.
        typing = []
        for mono_us, x, y in typed:
            t = to_recording_time(self.recorder.runs, mono_us / 1e6)
            if t is not None:
                typing.append([round(t, 4), round((x - g.x) / g.width, 5), round((y - g.y) / g.height, 5)])
        samples, clicks, was_down = [], [], 0
        for mono_us, x, y, down, *rest in raw:
            visible = rest[0] if rest else 1
            t = to_recording_time(self.recorder.runs, mono_us / 1e6)
            if t is None:
                was_down = down  # paused: don't turn this into a click
                continue
            samples.append([round(t, 4), round((x - g.x) / g.width, 5),
                            round((y - g.y) / g.height, 5), down, visible])
            if down and not was_down:
                clicks.append(round(t, 4))
            was_down = down
        (self.take_dir / "pointer.json").write_text(json.dumps(
            {"fps": settings.get("fps"), "samples": samples, "clicks": clicks, "typing": typing,
             "cursor": self.cursor, "scale": ui.monitor_scale(self.monitor)}))

    def cancel(self):
        """Abandons a recording that hasn't started yet."""
        if self.overlay is not None:
            overlay, self.overlay = self.overlay, None
            overlay._close(None if isinstance(overlay, ui.AreaSelector) else False)
            return  # its callback lands back in cancel()
        if self.recorder:
            self.recorder.on_finished = None
            self.recorder.stop()
        elif self.capture:
            self.capture.close()
        if self.take_dir:
            import shutil
            shutil.rmtree(self.take_dir, ignore_errors=True)
        self._reset()

    def _on_portal_closed(self):
        # GNOME's own "stop sharing" button in the top bar ends the recording.
        if self.recorder:
            self.recorder.mark_screen_ended()
        if self.phase == self.RECORDING:
            self.stop()
        elif self.phase == self.PREPARING:
            self.cancel()

    # ---- finish ---------------------------------------------------------------

    def _on_recorded(self, ok):
        if not ok:
            self.app.notify("The screen could not be recorded",
                            f"Nothing usable was written. The raw take is in {self.take_dir}")
            self._reset()
            return
        if settings.get("open_editor"):
            # The editor decides what to keep; it saves (or deletes) the take.
            export.write_take_info(self.take_dir, {**self._take_info(), "crop": self.crop})
            self.app.open_editor(self.take_dir, after_recording=True)
            self._reset()
            return
        dest = export.destination(settings.get("folder"))
        doc, info, duration = None, None, None
        if getattr(self, "cursor", "embedded") == "hidden":
            # Saving straight away: the pointer still has to be drawn back in.
            from . import edit_model as em
            from .editor import probe_duration
            duration = probe_duration(self.take_dir / "take.mkv")
            doc = em.new_document(duration)
            doc["pointer"]["drawn"] = True
            doc["zoom"]["enabled"] = False
            doc = em.normalize(doc, duration)
            info = {**self._take_info(), "crop": self.crop}
        export.save_async(self.take_dir / "take.mkv", dest, self.crop, self._on_saved,
                          doc=doc, take_info=info, duration=duration)

    def _on_saved(self, ok, message):
        if ok:
            self.app.notify("Recording saved", message.rsplit("/", 1)[-1], open_path=message)
            # Ready for the next recording.
            GLib.idle_add(lambda: self.app.show_chooser() and False)
        else:
            self.app.notify("The recording could not be saved",
                            f"{message}\nThe raw take is kept in {self.take_dir}")
        self._reset()

    def _close_pill(self):
        if self.tick:
            GLib.source_remove(self.tick)
            self.tick = None
        if self.pill:
            self.pill.destroy()
            self.pill = None
        if self.use_shell:
            self.shell.hide_controls()
            self.shell.hide_area()

    def _reset(self):
        if getattr(self, "tracking", False):
            self.shell.stop_pointer_track()
            self.tracking = False
        self._close_pill()
        was_active = self.phase != self.IDLE
        self.phase = self.IDLE
        self.capture = self.recorder = self.take_dir = self.crop = None
        if was_active:
            self.app.release()


class Framewise(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.session = None
        self.chooser = None
        self.bubble = None
        self.bubble_for_recording = False
        self.exit_code = 0
        open_action = Gio.SimpleAction.new("open-file", GLib.VariantType.new("s"))
        open_action.connect("activate", self._open_file)
        self.add_action(open_action)
        self.shots = None
        # Also what the top-bar menu and the icon's right-click menu run.
        for name, cb in (("preferences", self.show_settings), ("home", self.show_home),
                         ("record", self.record_toggle), ("screenshot", self.take_screenshot),
                         ("text", lambda: self.take_screenshot(text=True)),
                         ("clipboard", self.show_clipboard), ("camera", self.toggle_camera),
                         ("quit-app", self.quit_app)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=cb, n=name: (n == "quit-app" or self.stay_running(), cb()))
            self.add_action(action)
        for name, cb in (("shot-edit", self.edit_shot), ("shot-pin", self.pin_shot)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, param, cb=cb: cb(param.get_string()))
            self.add_action(action)

    def do_startup(self):
        Adw.Application.do_startup(self)
        ui.install_css()
        self.session = Session(self)
        export.sweep_old_takes()
        # First run for this person: shortcuts and the GNOME extension.
        from . import user_setup
        user_setup.ensure()

    def stay_running(self):
        """Like a menu-bar app: once started, Framewise keeps running (its icon
        at the top right, clipboard history) until Quit, windows open or not."""
        if not getattr(self, "_resident", False):
            self._resident = True
            self.hold()

    def do_activate(self):
        # Opened from the app grid.
        self.stay_running()
        self.show_home()

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if args and args[0] in ("-h", "--help", "help"):
            cmdline.print_literal(USAGE)
            return 0
        known = ("--background", "camera", "screenshot", "settings", "clipboard", "text", "edit", "record")
        if args and args[0] not in known:
            cmdline.printerr_literal(f"Unknown command: {args[0]}\n\n{USAGE}")
            return 2
        self.stay_running()
        if not args:
            self.show_home()
            return 0
        if args[0] == "--background":
            # Started at login: stay ready in the background, without a window.
            return 0
        if args[0] == "camera":
            self.toggle_camera()
            return 0
        if args[0] == "screenshot":
            delay = 0
            if "--delay" in args:
                try:
                    delay = max(0, int(args[args.index("--delay") + 1]))
                except (IndexError, ValueError):
                    cmdline.printerr_literal("--delay needs a number of seconds\n")
                    return 2
            self.take_screenshot(whole_screen="--screen" in args, delay=delay)
            return 0
        if args[0] == "settings":
            self.show_settings()
            return 0
        if args[0] == "clipboard":
            self.show_clipboard()
            return 0
        if args[0] == "text":
            self.take_screenshot(text=True)
            return 0
        if args[0] == "edit" and len(args) > 1:
            path = cmdline.create_file_for_arg(args[1]).get_path()
            if not path or not (Path(path) / "take.mkv").exists():
                cmdline.printerr_literal(f"No recording found in {args[1]}\n")
                return 1
            self.open_editor(path)
            return 0
        if args[0] != "record":
            cmdline.printerr_literal(f"Unknown command: {args[0]}\n\n{USAGE}")
            return 2
        flag = args[1] if len(args) > 1 else ""
        s = self.session
        if flag == "--stop":
            s.stop()
        elif flag == "--pause":
            s.toggle_pause()
        elif s.phase != Session.IDLE:
            s.stop()
        elif flag == "--now":
            s.begin(settings.get("last_mode"))
        else:
            self.show_chooser()
        return 0

    def show_chooser(self):
        if self.chooser is not None:
            self.chooser.present()
            return
        self.chooser = ui.Chooser(self, self._start_from_chooser,
                                  camera_open=self.camera_open,
                                  on_camera=self._on_camera_switch,
                                  on_preview=self.preview_camera)
        self.chooser.connect("close-request", self._chooser_closed)
        self.chooser.present()

    def show_home(self):
        from .home import Home
        if getattr(self, "home", None) is not None:
            self.home.present()
            return
        self.home = Home(self)
        self.home.connect("close-request", lambda *_: setattr(self, "home", None) or False)
        self.home.present()

    def show_clipboard(self):
        shell = self.session.shell
        if shell.has_clipboard:
            shell.show_clipboard()
        else:
            self.notify("Clipboard history needs the Framewise extension",
                        "Log out and back in after installing or updating Framewise.")

    def quit_app(self):
        """Quit from the top-bar menu: a recording is stopped and saved first,
        and windows with unsaved work still ask before closing."""
        s = self.session
        if s.phase in (Session.RECORDING, Session.PREPARING):
            s.stop() if s.phase == Session.RECORDING else s.cancel()
        if s.phase != Session.IDLE:
            # Still saving the recording: try again in a moment.
            GLib.timeout_add(400, lambda: self.quit_app() and False)
            return
        if getattr(self, "_resident", False):
            self._resident = False
            self.release()
        for win in list(self.get_windows()):
            win.close()

        def finish():
            # A window that asked "save?" is still open: let the person answer.
            if not any(w.get_visible() for w in self.get_windows()):
                self.quit()
            return False
        GLib.timeout_add(300, finish)

    def record_toggle(self):
        """Opens the record window, or stops the recording that is running."""
        s = self.session
        if s.phase != Session.IDLE:
            s.stop()
        else:
            self.show_chooser()

    def show_settings(self):
        from .settings_window import SettingsWindow
        if getattr(self, "settings_window", None) is not None:
            self.settings_window.present()
            return
        self.hold()
        self.settings_window = SettingsWindow(self)

        def closed(*_):
            self.settings_window = None
            self.release()
            return False
        self.settings_window.connect("close-request", closed)
        self.settings_window.present()

    # ---- screenshots -----------------------------------------------------------------

    def take_screenshot(self, whole_screen=False, delay=0, text=False):
        if self.session.phase != Session.IDLE:
            return   # not while recording: the picker would cover the recording
        if self.shots is None:
            from .shots import ShotSession
            self.shots = ShotSession(self)
        self.shots.start(whole_screen=whole_screen, delay=delay, text=text)

    def edit_shot(self, path):
        from .shot_editor import ShotEditor
        if not Path(path).exists():
            self.notify("Screenshot not found", path)
            return
        self.hold()
        editor = ShotEditor(self, path, on_closed=self.release, on_pin=self.pin_shot)
        editor.present()

    def pin_shot(self, path):
        from .pin import Pin
        if not Path(path).exists():
            return
        self.hold()
        pin = Pin(self, path, on_edit=self.edit_shot, on_closed=self.release)
        pin.show_pinned(self.session.shell)

    # ---- editor --------------------------------------------------------------------

    def open_editor(self, take_dir, after_recording=False):
        from .editor import Editor
        self.hold()  # keep running while an editor is open
        # After a new recording is saved, the record window comes back, ready
        # for the next one.
        editor = Editor(self, take_dir, on_closed=self.release,
                        on_saved=self.show_chooser if after_recording else None)
        editor.present()

    # ---- camera bubble ---------------------------------------------------------

    @property
    def camera_open(self):
        return self.bubble is not None

    def toggle_camera(self):
        if self.bubble:
            self.bubble.close()
        else:
            self.show_camera()

    def show_camera(self, for_recording=False):
        """for_recording: opened for a recording (closes when it ends); else
        opened on its own, e.g. as a mirror, and left open."""
        if self.bubble:
            self.bubble.present()
            return
        self.bubble_for_recording = for_recording
        from .bubble import CameraBubble
        self.hold()  # keep running while the bubble is open
        self.bubble = CameraBubble(self, on_closed=self._camera_closed)
        self.bubble.open()
        self.keep_camera_above()

    def keep_camera_above(self):
        """Above every window, on every workspace (needs the extension). GNOME
        only finds the window once it is on screen, so this tries until it does."""
        from .bubble import TITLE
        shell = self.session.shell
        if not shell.has_keep_above:
            return
        tries = [0]

        def attempt():
            tries[0] += 1
            if self.bubble is None:
                return False
            return not shell.keep_above(TITLE) and tries[0] < 20
        GLib.timeout_add(150, attempt)

    def _on_camera_switch(self, device_id):
        """device_id: the camera picked in the record window, or None for Off.
        The bubble opens when recording starts (or with Preview), not here."""
        settings.set("camera", device_id is not None)
        if device_id is None:
            if self.bubble:
                self.bubble.close()
            return
        changed = settings.get("camera_device") != device_id
        settings.set("camera_device", device_id)
        if self.bubble and changed:
            # A different camera while previewing: reopen it, in the same place.
            owner = self.bubble_for_recording
            self.bubble.close()
            GLib.timeout_add(300, lambda: self.show_camera(for_recording=owner) and False)

    def preview_camera(self, on):
        if on and not self.bubble:
            self.show_camera(for_recording=True)
        elif not on and self.bubble:
            self.bubble.close()

    def _camera_closed(self):
        self.bubble = None
        if self.chooser is not None:
            self.chooser.preview.set_active(False)
        self.release()

    def _chooser_closed(self, *_):
        # Cancelled: a camera preview goes away with the record window.
        if not self.chooser.started and self.bubble:
            self.bubble.close()
        self.chooser = None
        return False

    def _start_from_chooser(self, mode):
        self.chooser = None
        self.session.begin(mode)

    def notify(self, title, body, open_path=None):
        n = Gio.Notification.new(title)
        n.set_body(body)
        if open_path:
            folder = open_path.rsplit("/", 1)[0]
            n.set_default_action(Gio.Action.print_detailed_name("app.open-file", GLib.Variant("s", open_path)))
            n.add_button("Show in folder",
                         Gio.Action.print_detailed_name("app.open-file", GLib.Variant("s", folder)))
        self.send_notification(None, n)
        print(f"{title}: {body}", file=sys.stderr, flush=True)

    OPENABLE = {".mp4", ".mkv", ".gif", ".png", ".jpg", ".jpeg", ".webp"}

    def _open_file(self, _action, param):
        """Opens a saved recording or picture, or a folder (from notifications).
        Any other kind of file is refused: this action can be triggered from
        outside the app, and opening e.g. a launcher file could run it."""
        path = Path(param.get_string())
        if not path.is_absolute() or not path.exists():
            return
        if not (path.is_dir() or path.suffix.lower() in self.OPENABLE):
            return
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(str(path)), None)


def main(argv=None):
    app = Framewise()
    return app.run(argv if argv is not None else sys.argv)
