"""Windows for the recorder: chooser, area selection, countdown, controls pill."""

import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from .state import settings  # noqa: E402

CSS = b"""
window.ck-overlay, window.ck-overlay > * { background: transparent; }
.ck-pill { padding: 6px 10px; }
.ck-dot { color: #e5484d; font-size: 14px; }
.ck-dot.paused { color: #f5a623; }
.ck-time { font-feature-settings: "tnum"; font-weight: 600; min-width: 4.5em; }
.ck-mode button { padding: 10px 14px; }
"""


def install_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def monitor_for(stream):
    """The Gdk monitor that shows a portal stream, matched by its position."""
    monitors = Gdk.Display.get_default().get_monitors()
    best = None
    for i in range(monitors.get_n_items()):
        m = monitors.get_item(i)
        g = m.get_geometry()
        if best is None:
            best = m
        if (g.x, g.y) == (stream.x, stream.y):
            return m
    return best


def list_monitors():
    """[(connector, label, monitor)] for every screen, e.g. ("HDMI-1", "LC34G55T · 3440 × 1440", m)."""
    result = []
    monitors = Gdk.Display.get_default().get_monitors()
    for i in range(monitors.get_n_items()):
        m = monitors.get_item(i)
        g = m.get_geometry()
        scale = monitor_scale(m)
        connector = m.get_connector() or f"screen-{i}"
        name = "Built-in screen" if connector.startswith(("eDP", "LVDS", "DSI")) else (
            m.get_model() or m.get_description() or connector)
        size = f"{round(g.width * scale)} × {round(g.height * scale)}"
        result.append((connector, f"{name} · {size}", m))
    return result


def monitor_by_connector(connector):
    for c, _label, m in list_monitors():
        if c == connector:
            return m
    return None


def monitor_scale(monitor):
    try:
        return monitor.get_scale()
    except AttributeError:
        return monitor.get_scale_factor()


def format_time(seconds):
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _overlay_window(app, monitor, click_through=False):
    win = Gtk.Window(application=app, decorated=False)
    win.add_css_class("ck-overlay")
    win.fullscreen_on_monitor(monitor)
    if click_through:
        def on_realize(w):
            surface = w.get_surface()
            if surface:
                surface.set_input_region(cairo.Region())
        win.connect("realize", on_realize)
    return win


# ---------------------------------------------------------------------------
# Chooser


class Chooser(Adw.ApplicationWindow):
    """Pick what to record and which sound to include, then press Record."""

    def __init__(self, app, on_record, camera_open=False, on_camera=None, on_preview=None):
        super().__init__(application=app, title="Record", resizable=False)
        self.on_record = on_record
        self.started = False      # closed by Record (not cancelled)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="New Recording"))
        header.pack_end(Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings",
                                   action_name="app.preferences"))

        modes = Gtk.Box(spacing=0, homogeneous=True, css_classes=["linked", "ck-mode"])
        self.mode_buttons = {}
        first = None
        for key, label, icon in (("area", "Area", "selection-mode-symbolic"),
                                 ("window", "Window", "focus-windows-symbolic"),
                                 ("screen", "Screen", "video-display-symbolic")):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(Gtk.Image(icon_name=icon, pixel_size=24))
            box.append(Gtk.Label(label=label))
            b = Gtk.ToggleButton(child=box)
            if first is None:
                first = b
            else:
                b.set_group(first)
            self.mode_buttons[key] = b
            modes.append(b)
        self.mode_buttons.get(settings.get("last_mode"), self.mode_buttons["screen"]).set_active(True)

        prefs = Adw.PreferencesGroup()
        self.system_row = Adw.SwitchRow(title="System audio", subtitle="Recorded on its own track")
        self.system_row.set_active(settings.get("system_audio"))
        # Microphone: Off, the system default, or a specific one.
        from . import devices
        self.mics = devices.microphones()
        self.mic_row = Adw.ComboRow(title="Microphone", subtitle="Recorded on its own track")
        self.mic_row.set_model(Gtk.StringList.new(
            ["Off", "Default"] + [d.name for d in self.mics]))
        chosen = settings.get("microphone_device") or ""
        ids = [d.id for d in self.mics]
        if not settings.get("microphone"):
            self.mic_row.set_selected(0)
        else:
            self.mic_row.set_selected(2 + ids.index(chosen) if chosen in ids else 1)
        self.countdown_row = Adw.ComboRow(title="Countdown")
        options = [0, 3, 5, 10]
        self.countdown_values = options
        self.countdown_row.set_model(Gtk.StringList.new(["Off", "3 s", "5 s", "10 s"]))
        cd = settings.get("countdown")
        self.countdown_row.set_selected(options.index(cd) if cd in options else 1)
        self.screens = list_monitors()
        self.screen_row = Adw.ComboRow(title="Display",
                                       subtitle="The display to record")
        self.screen_row.set_model(Gtk.StringList.new([label for _c, label, _m in self.screens]))
        self.screen_row.set_visible(len(self.screens) > 1)
        saved = settings.get("screen")
        connectors = [c for c, _l, _m in self.screens]
        self._screen_chosen = saved in connectors
        if self._screen_chosen:
            self.screen_row.set_selected(connectors.index(saved))
        else:
            # Default to the screen this window opens on.
            self.connect("map", self._default_screen)
        self.quality_row = Adw.ComboRow(title="Quality")
        self.quality_values = ["max", "high", "balanced"]
        self.quality_row.set_model(Gtk.StringList.new(["Maximum", "High", "Balanced"]))
        q = settings.get("quality")
        self.quality_row.set_selected(self.quality_values.index(q) if q in self.quality_values else 1)
        # Camera: Off or one of the cameras; choosing one opens the bubble.
        self.cams = devices.cameras()
        self.camera_row = Adw.ComboRow(title="Camera",
                                       subtitle="Shown when recording starts")
        self.camera_row.set_model(Gtk.StringList.new(["Off"] + [d.name for d in self.cams]))
        cam_ids = [d.id for d in self.cams]
        current = settings.get("camera_device")
        if settings.get("camera") and self.cams:
            self.camera_row.set_selected(1 + (cam_ids.index(current) if current in cam_ids else 0))
        # The bubble appears when recording starts; Preview shows it now, to place it.
        self.preview = Gtk.ToggleButton(label="Preview", valign=Gtk.Align.CENTER, active=camera_open,
                                        tooltip_text="Show the camera to position it before recording")
        self.preview.set_sensitive(self.camera_row.get_selected() != 0)
        if on_preview:
            self.preview.connect("toggled", lambda b: on_preview(b.get_active()))
        self.camera_row.add_suffix(self.preview)
        if not self.cams:
            self.camera_row.set_subtitle("No camera found")
            self.camera_row.set_sensitive(False)

        def camera_changed(row, _p):
            i = row.get_selected()
            self.preview.set_sensitive(i != 0)
            if i == 0:
                self.preview.set_active(False)
            if on_camera:
                on_camera(None if i == 0 else self.cams[i - 1].id)
        self.camera_row.connect("notify::selected", camera_changed)
        for row in (self.screen_row, self.system_row, self.mic_row, self.camera_row,
                    self.countdown_row, self.quality_row):
            prefs.add(row)

        record = Gtk.Button(label="Record", css_classes=["suggested-action", "pill"])
        record.connect("clicked", self._on_record)
        change = Gtk.Button(label="Reset Screen Permissions", css_classes=["flat"])
        change.connect("clicked", self._on_forget)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        body.append(modes)
        body.append(prefs)
        body.append(record)
        body.append(change)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_content(view)
        self.set_default_size(380, -1)
        self.set_default_widget(record)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _default_screen(self, *_):
        surface = self.get_surface()
        here = self.get_display().get_monitor_at_surface(surface) if surface else None
        for i, (_c, _l, m) in enumerate(self.screens):
            if m == here:
                self.screen_row.set_selected(i)
                return

    def _on_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_forget(self, button):
        from . import portal
        portal.forget_screen()
        button.set_label("Screen permissions reset")
        button.set_sensitive(False)

    def _on_record(self, _b):
        mode = next(k for k, b in self.mode_buttons.items() if b.get_active())
        settings.set("last_mode", mode)
        if self.screens:
            settings.set("screen", self.screens[self.screen_row.get_selected()][0])
        settings.set("system_audio", self.system_row.get_active())
        mic = self.mic_row.get_selected()
        settings.set("microphone", mic != 0)
        settings.set("microphone_device", self.mics[mic - 2].id if mic >= 2 else "")
        settings.set("countdown", self.countdown_values[self.countdown_row.get_selected()])
        settings.set("quality", self.quality_values[self.quality_row.get_selected()])
        self.started = True
        self.close()
        self.on_record(mode)


# ---------------------------------------------------------------------------
# Area selection


class AreaSelector:
    """A dimmed full-screen layer where the user drags out a rectangle.

    Enter reuses the last area, Esc cancels. Calls on_done((x, y, w, h)) as
    fractions (0 to 1) of the monitor, so the result doesn't depend on display
    scaling, or on_done(None) when cancelled.
    """

    MIN_SIDE = 32

    def __init__(self, app, monitor, on_done, still=None):
        """still: a Gdk.Texture of the screen, shown dimmed behind the selection."""
        self.on_done = on_done
        self.still = still
        self.scale = monitor_scale(monitor)
        self.start = None
        self.rect = None  # in widget (logical) units
        self.finished = False

        self.win = _overlay_window(app, monitor)
        area = _SelectionCanvas(self)
        area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
        self.area = area

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        area.add_controller(drag)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.win.add_controller(keys)
        self.win.set_child(area)
        self.win.present()

    def _begin(self, _g, x, y):
        self.start = (x, y)
        self.rect = (x, y, 0, 0)
        self.area.queue_draw()

    def _update(self, _g, dx, dy):
        x0, y0 = self.start
        x1, y1 = x0 + dx, y0 + dy
        self.rect = (min(x0, x1), min(y0, y1), abs(dx), abs(dy))
        self.area.queue_draw()

    def _end(self, _g, _dx, _dy):
        if not self.rect:
            return
        px = self._to_pixels(self.rect)
        if px[2] < self.MIN_SIDE or px[3] < self.MIN_SIDE:
            self.rect = None  # too small: treat as a stray click
            self.area.queue_draw()
            return
        w, h = self.area.get_width(), self.area.get_height()
        x, y, rw, rh = self.rect
        frac = [x / w, y / h, rw / w, rh / h]
        settings.set("last_area", frac)
        self._close(tuple(frac))

    def _key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self._close(None)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            last = settings.get("last_area")
            if last:
                self._close(tuple(last))
            return True
        return False

    def _to_pixels(self, rect):
        x, y, w, h = (v * self.scale for v in rect)
        x, y = int(round(x)) & ~1, int(round(y)) & ~1
        w, h = int(round(w)) & ~1, int(round(h)) & ~1
        return (x, y, w, h)

    def _close(self, result):
        if self.finished:
            return
        self.finished = True
        self.win.destroy()
        self.on_done(result)


SELECTION_RED = Gdk.RGBA(red=0.898, green=0.282, blue=0.302, alpha=1.0)
DIM = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.5)
BADGE_BG = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.78)
WHITE = Gdk.RGBA(red=1, green=1, blue=1, alpha=1)


def _rect(x, y, w, h):
    r = Graphene.Rect()
    r.init(x, y, w, h)
    return r


class _SelectionCanvas(Gtk.Widget):
    """Draws the selector with GTK's GPU renderer.

    The still of the screen is a texture uploaded once; each mouse move only
    redraws a few rectangles on top of it, which keeps dragging smooth even
    on a 3440-pixel-wide screen.
    """

    def __init__(self, selector):
        super().__init__(hexpand=True, vexpand=True)
        self.sel = selector

    def do_snapshot(self, snap):
        width, height = self.get_width(), self.get_height()
        if self.sel.still is not None:
            snap.append_texture(self.sel.still, _rect(0, 0, width, height))
        rect = self.sel.rect
        if not rect or rect[2] < 1 or rect[3] < 1:
            snap.append_color(DIM, _rect(0, 0, width, height))
            self._badge(snap, "Drag to choose the area to record   ·   "
                              "Enter: last area   ·   Esc: cancel",
                        width / 2, height / 2, width, height, center=True)
            return
        x, y, w, h = rect
        # Dim everything except the selection: four bands around it.
        snap.append_color(DIM, _rect(0, 0, width, y))
        snap.append_color(DIM, _rect(0, y + h, width, height - y - h))
        snap.append_color(DIM, _rect(0, y, x, h))
        snap.append_color(DIM, _rect(x + w, y, width - x - w, h))
        # Red outline just outside the selection, like the recording border.
        bw = 2
        outline = Gsk.RoundedRect()
        outline.init_from_rect(_rect(x - bw, y - bw, w + 2 * bw, h + 2 * bw), 0)
        snap.append_border(outline, [bw] * 4, [SELECTION_RED] * 4)
        px = self.sel._to_pixels(rect)
        self._badge(snap, f"{px[2]} × {px[3]}", x, y + h + 10, width, height)

    def _badge(self, snap, text, x, y, width, height, center=False):
        layout = self.create_pango_layout(None)
        layout.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>", -1)
        _ink, logical = layout.get_pixel_extents()
        pad_x, pad_y = 10, 6
        bw, bh = logical.width + 2 * pad_x, logical.height + 2 * pad_y
        if center:
            x, y = x - bw / 2, y - bh / 2
        x = min(max(6, x), width - bw - 6)
        y = min(max(6, y), height - bh - 6)
        bg = Gsk.RoundedRect()
        bg.init_from_rect(_rect(x, y, bw, bh), 8)
        snap.push_rounded_clip(bg)
        snap.append_color(BADGE_BG, _rect(x, y, bw, bh))
        snap.pop()
        point = Graphene.Point()
        point.init(x + pad_x, y + pad_y)
        snap.save()
        snap.translate(point)
        snap.append_layout(layout, WHITE)
        snap.restore()


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# ---------------------------------------------------------------------------
# Countdown


class Countdown:
    """3-2-1 in a small window, used when the Shell extension isn't running.

    A small window, not a full-screen one: GNOME draws see-through
    full-screen windows as black. Calls on_done(True) when it reaches zero,
    on_done(False) if Esc is pressed.
    """

    def __init__(self, app, monitor, seconds, on_done):
        self.left = seconds
        self.on_done = on_done
        self.finished = False
        self.win = Gtk.Window(application=app, decorated=False, resizable=False,
                              title="Recording starts")
        self.win.set_default_size(220, 220)
        area = Gtk.DrawingArea()
        area.set_draw_func(self._draw)
        self.area = area
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.win.add_controller(keys)
        self.win.set_child(area)
        self.win.present()
        self.timer = GLib.timeout_add(1000, self._tick)

    def _tick(self):
        self.left -= 1
        if self.left <= 0:
            self.timer = None
            self._close(True)
            return False
        self.area.queue_draw()
        return True

    def _key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self._close(False)
            return True
        return False

    def _close(self, ok):
        if self.finished:
            return
        self.finished = True
        if self.timer:
            GLib.source_remove(self.timer)
        self.win.destroy()
        self.on_done(ok)

    def _draw(self, _a, cr, width, height):
        r = 90
        cx, cy = width / 2, height / 2
        cr.set_source_rgba(0, 0, 0, 0.6)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(96)
        text = str(self.left)
        ext = cr.text_extents(text)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
        cr.show_text(text)
        cr.set_font_size(12)
        hint = "Esc to cancel"
        ext = cr.text_extents(hint)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + 62)
        cr.show_text(hint)


# ---------------------------------------------------------------------------
# Controls pill


class ControlsPill(Gtk.Window):
    """Elapsed time with pause/resume and stop."""

    def __init__(self, app, on_pause_toggle, on_stop):
        super().__init__(application=app, title="Recording", resizable=False,
                         deletable=False)
        self.set_titlebar(Gtk.Box())  # no title bar: it's a small pill
        self.dot = Gtk.Label(label="●", css_classes=["ck-dot"])
        self.time = Gtk.Label(label="0:00", css_classes=["ck-time"], xalign=0)
        self.pause_btn = Gtk.Button(icon_name="media-playback-pause-symbolic",
                                    tooltip_text="Pause recording", css_classes=["flat", "circular"])
        self.pause_btn.connect("clicked", lambda *_: on_pause_toggle())
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop recording",
                          css_classes=["destructive-action", "circular"])
        stop.connect("clicked", lambda *_: on_stop())
        box = Gtk.Box(spacing=8, css_classes=["ck-pill"], valign=Gtk.Align.CENTER)
        for w in (self.dot, self.time, self.pause_btn, stop):
            box.append(w)
        handle = Gtk.WindowHandle(child=box)  # drag the pill to move it
        self.set_child(handle)

    def update(self, seconds, paused):
        self.time.set_label(format_time(seconds))
        if paused:
            self.dot.add_css_class("paused")
            self.pause_btn.set_icon_name("media-record-symbolic")
            self.pause_btn.set_tooltip_text("Resume recording")
        else:
            self.dot.remove_css_class("paused")
            self.pause_btn.set_icon_name("media-playback-pause-symbolic")
            self.pause_btn.set_tooltip_text("Pause recording")
