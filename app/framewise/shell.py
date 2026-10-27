"""Talks to framewise's GNOME Shell extension over D-Bus.

When the extension is running, recording controls live in the top bar and
a border marks the recorded area, both above every window. When it isn't,
callers fall back to the app's own controls window.
"""

from gi.repository import Gio, GLib

BUS_NAME = "com.gitcotech.Framewise.Shell"
OBJECT_PATH = "/com/gitcotech/Framewise/Shell"
INTERFACE = "com.gitcotech.Framewise.Shell"


class ShellHelper:
    def __init__(self):
        self.on_pause = None
        self.on_stop = None
        self.on_countdown_cancelled = None
        self.on_shot_action = None
        self.on_bubble_corner = None
        self._proxy = None
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None, BUS_NAME, OBJECT_PATH, INTERFACE, None)
            self._proxy.connect("g-signal", self._on_signal)
        except GLib.Error:
            self._proxy = None

    @property
    def available(self):
        return self._proxy is not None and self._proxy.get_name_owner() is not None

    def version(self):
        """The running extension's version as a tuple, or () if not running."""
        if not self.available:
            return ()
        try:
            res = self._proxy.call_sync("Ping", None, Gio.DBusCallFlags.NONE, 1000, None)
            return tuple(int(p) for p in res.unpack()[0].split("."))
        except (GLib.Error, ValueError):
            return ()

    @property
    def has_countdown(self):
        return self.version() >= (0, 2)

    @property
    def has_keep_above(self):
        return self.version() >= (0, 4)

    def keep_above(self, title):
        """True once GNOME found the window and put it above the others."""
        res = self._call_sync("KeepAbove", GLib.Variant("(s)", (title,)), timeout=1000)
        return bool(res and res[0])

    @property
    def has_pointer_track(self):
        return self.version() >= (0, 5)

    def start_pointer_track(self):
        self._call("StartPointerTrack")

    def stop_pointer_track(self):
        """{"pointer": [[monotonic_us, x, y, down, visible], ...],
            "typing": [[monotonic_us, x, y], ...]} in screen coordinates.
        Extensions before 1.7 sent only the pointer list."""
        if not self.available:
            return []
        try:
            res = self._proxy.call_sync("StopPointerTrack", None, Gio.DBusCallFlags.NONE, 5000, None)
            import json
            return json.loads(res.unpack()[0])
        except (GLib.Error, ValueError):
            return []

    @property
    def has_drawn_pointer(self):
        # 0.6 reports whether the pointer is visible, which drawing it needs.
        return self.version() >= (0, 6)

    @property
    def has_borders(self):
        # 0.1 could leave a border stuck on screen; 0.3 fixed it and added
        # window and screen borders.
        return self.version() >= (0, 3)

    def _call(self, method, params=None):
        if not self.available:
            return
        self._proxy.call(method, params, Gio.DBusCallFlags.NONE, 1000, None, None)

    def _on_signal(self, _proxy, _sender, signal, _params):
        if signal == "PauseClicked" and self.on_pause:
            self.on_pause()
        elif signal == "StopClicked" and self.on_stop:
            self.on_stop()
        elif signal == "CountdownCancelled" and self.on_countdown_cancelled:
            self.on_countdown_cancelled()
        elif signal == "ShotAction" and self.on_shot_action:
            self.on_shot_action(_params.unpack()[0])
        elif signal == "BubbleCornerChanged" and self.on_bubble_corner:
            self.on_bubble_corner(_params.unpack()[0])

    def show_area(self, x, y, w, h):
        self._call("ShowArea", GLib.Variant("(iiii)", (int(x), int(y), int(w), int(h))))

    def show_screen_border(self, x, y, w, h):
        self._call("ShowScreenBorder", GLib.Variant("(iiii)", (int(x), int(y), int(w), int(h))))

    def show_window_border(self, w, h):
        self._call("ShowWindowBorder", GLib.Variant("(ii)", (int(w), int(h))))

    def hide_area(self):
        self._call("HideArea")

    def show_controls(self):
        self._call("ShowControls")

    def update_controls(self, elapsed, paused):
        self._call("UpdateControls", GLib.Variant("(db)", (float(elapsed), bool(paused))))

    def hide_controls(self):
        self._call("HideControls")

    @property
    def has_controls_rect(self):
        # 1.11 floats the controls in one fixed spot and says where.
        return self.version() >= (1, 11)

    def controls_rect(self):
        """(x, y, w, h) of the recording controls on screen, or None."""
        res = self._call_sync("ControlsRect", None, 1000)
        return tuple(res) if res and res[2] > 0 and res[3] > 0 else None

    def show_countdown(self, center_x, center_y, seconds):
        self._call("ShowCountdown", GLib.Variant("(iii)", (int(center_x), int(center_y), int(seconds))))

    def hide_countdown(self):
        self._call("HideCountdown")

    # ---- screenshots (0.7) ------------------------------------------------

    @property
    def has_screenshots(self):
        return self.version() >= (0, 7)

    def _call_sync(self, method, params, timeout=8000):
        if not self.available:
            return None
        try:
            return self._proxy.call_sync(method, params, Gio.DBusCallFlags.NONE, timeout, None).unpack()
        except GLib.Error:
            return None

    def screenshot(self, path, cursor=False):
        """The whole desktop as a PNG at path. True when it worked."""
        res = self._call_sync("Screenshot", GLib.Variant("(bs)", (bool(cursor), str(path))))
        return bool(res and res[0])

    def screenshot_window(self, window_id, path, cursor=False):
        res = self._call_sync("ScreenshotWindow",
                              GLib.Variant("(tbs)", (int(window_id), bool(cursor), str(path))))
        return bool(res and res[0])

    def list_windows(self):
        """[{id, x, y, w, h, title}] top-most first, in desktop coordinates."""
        res = self._call_sync("ListWindows", None, 2000)
        if not res:
            return []
        import json
        try:
            return json.loads(res[0])
        except ValueError:
            return []

    def show_shot(self, path, width, height, at_x, at_y, seconds, note=""):
        self._call("ShowShot", GLib.Variant("(siiiius)", (str(path), int(width), int(height),
                                                          int(at_x), int(at_y), int(seconds), note)))

    def hide_shot(self):
        self._call("HideShot")

    @property
    def has_clipboard(self):
        return self.version() >= (0, 8)

    def show_clipboard(self):
        self._call("ShowClipboard")

    # ---- camera bubble corner (1.8) ------------------------------------------

    @property
    def has_bubble_corner(self):
        return self.version() >= (1, 8)

    def bubble_corner(self):
        """The screen corner the camera bubble is nearest, e.g. "bottom-right"."""
        res = self._call_sync("BubbleCorner", None, 1000)
        return res[0] if res else "bottom-right"

    def set_bubble_aspect(self, ratio, margin):
        self._call("SetBubbleAspect", GLib.Variant("(di)", (float(ratio), int(margin))))


class ShellCountdown:
    """3-2-1 drawn by the extension over the live screen.

    Same interface as ui.Countdown: on_done(True) at zero, on_done(False)
    when cancelled (by a click on it, or through _close).
    """

    def __init__(self, shell, monitor, seconds, on_done):
        self.shell = shell
        self.left = seconds
        self.on_done = on_done
        self.finished = False
        g = monitor.get_geometry()
        self.center = (g.x + g.width / 2, g.y + g.height / 2)
        shell.on_countdown_cancelled = lambda: self._close(False)
        shell.show_countdown(*self.center, self.left)
        self.timer = GLib.timeout_add(1000, self._tick)

    def _tick(self):
        self.left -= 1
        if self.left <= 0:
            self.timer = None
            self._close(True)
            return False
        self.shell.show_countdown(*self.center, self.left)
        return True

    def _close(self, ok):
        if self.finished:
            return
        self.finished = True
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        self.shell.on_countdown_cancelled = None
        self.shell.hide_countdown()
        self.on_done(bool(ok))
