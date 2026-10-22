"""The floating camera bubble.

A small borderless window showing the webcam in a circle, a rounded
rectangle or a square. Drag it anywhere, resize it from GNOME's window menu
(right-click or Alt+F8), and hover to reach its controls (shape, look, adjustments, focus lock, mirror, close). It is
an ordinary window on screen, so screen and area recordings capture it
wherever it is, including when it is moved or resized mid-recording.
"""

import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .camera import DEFAULT_PRESET, OLD_LOOKS, PRESETS, Camera  # noqa: E402
from .state import settings  # noqa: E402

TITLE = "framewise camera"

SHAPES = {
    # name: (aspect ratio, label, icon)
    "circle": (1.0, "Circle", "media-record-symbolic"),
    "rounded": (4 / 3, "Rounded", "view-paged-symbolic"),
    "square": (1.0, "Square", "checkbox-symbolic"),
}
SHAPE_ORDER = ["circle", "rounded", "square"]


# Camera preview style: black surface, 14 px corners, a
# hairline white edge at 14 % opacity, a soft shadow, and controls in a
# translucent black capsule that fades in on hover (0.15 s).
CSS = """
window.ck-bubble, window.ck-bubble > * { background: transparent; box-shadow: none; }
.ck-cam-frame { background: #000; border: 1px solid rgba(255,255,255,0.14);
                box-shadow: 0 8px 24px rgba(0,0,0,0.38); margin: 14px; }
.ck-cam-frame.circle { border-radius: 9999px; }
.ck-cam-frame.square { border-radius: 0; }
.ck-cam-controls { background: rgba(0,0,0,0.55); border-radius: 999px; padding: 3px;
                   transition: opacity 150ms ease-in-out; }
.ck-cam-controls.ck-hidden { opacity: 0; }
.ck-cam-controls > button, .ck-cam-controls > menubutton > button { color: white; min-width: 28px; min-height: 28px; }
.ck-cam-status { color: rgba(255,255,255,0.8); font-size: 0.85em; }
.ck-cam-status image { color: rgba(255,255,255,0.55); }
"""

DEFAULTS = {
    "camera_shape": "circle",
    "camera_size": [260, 260],
    "camera_look": DEFAULT_PRESET,
    "camera_mirror": True,
    "camera_radius": 14,
    "camera_brightness": 0.0,
    "camera_contrast": 1.0,
    "camera_saturation": 1.0,
    "camera_blur": 0.0,           # effects, 0..1 (camera_fx)
    "camera_smooth": 0.0,
    "camera_light": 0.0,
    "camera_focus_lock": False,   # cameras with autofocus only
}


def _get(key):
    value = settings.get(key)
    return DEFAULTS[key] if value is None else value


class CameraBubble(Gtk.Window):
    def __init__(self, app, on_closed=None):
        super().__init__(application=app, title=TITLE, decorated=False, resizable=True)
        self.add_css_class("ck-bubble")
        self.on_closed = on_closed
        self.camera = Camera(settings.get("camera_device"))
        self.camera.on_error = self._on_camera_error

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._radius_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._radius_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.frame = Gtk.Box(css_classes=["ck-cam-frame"], hexpand=True, vexpand=True)
        self.frame.set_overflow(Gtk.Overflow.HIDDEN)  # clip the video to the shape
        self.frame.append(self.picture)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        self.aspect = Gtk.AspectFrame(obey_child=False, xalign=0.5, yalign=0.5, child=self.frame)
        self.message = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                               css_classes=["ck-cam-status"], halign=Gtk.Align.CENTER,
                               valign=Gtk.Align.CENTER, margin_start=28, margin_end=28,
                               visible=False)
        self.message.append(Gtk.Image(icon_name="camera-disabled-symbolic", pixel_size=26))
        self.message_text = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.message.append(self.message_text)
        self.spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER, width_request=20, height_request=20)

        overlay = Gtk.Overlay(child=self.aspect)
        overlay.add_overlay(self.spinner)
        overlay.add_overlay(self.message)
        self.controls = self._build_controls()
        overlay.add_overlay(self.controls)

        handle = Gtk.WindowHandle(child=overlay)  # drag anywhere to move
        self.set_child(handle)

        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: self._show_controls(True))
        hover.connect("leave", lambda *_: self._show_controls(False))
        self.add_controller(hover)
        self._show_controls(False)

        w, h = _get("camera_size")
        self.set_default_size(int(w), int(h))
        self.connect("close-request", self._on_close)
        self.connect("realize", self._on_realize)
        self._ready = False
        self._ready_waiters = []

        self._apply_shape(_get("camera_shape"))
        self._apply_radius(_get("camera_radius"))

    # ---- life -----------------------------------------------------------------

    def open(self):
        self.camera.set_effects(blur=_get("camera_blur"), smooth=_get("camera_smooth"),
                                light=_get("camera_light"))
        self.camera.on_paintable = self._on_new_paintable
        self.camera.set_focus_lock(_get("camera_focus_lock"))
        try:
            self.camera.start()
        except GLib.Error as e:
            self._on_camera_error(e.message)
        if self.camera.paintable is not None:
            self.picture.set_paintable(self.camera.paintable)
            # Hide the spinner once the first picture arrives.
            self._first_frame = self.camera.paintable.connect(
                "invalidate-contents", self._on_first_frame)
        self.camera.set_look(_get("camera_look"))
        self.camera.set_mirror(_get("camera_mirror"))
        self.camera.set_adjustments(_get("camera_brightness"), _get("camera_contrast"),
                                    _get("camera_saturation"))
        self.present()

    def _on_close(self, *_):
        settings.set("camera_size", [self.get_width(), self.get_height()])
        self.camera.stop()
        if self.on_closed:
            self.on_closed()
        return False

    def _on_new_paintable(self, paintable):
        """Effects turned on or off: the camera restarted with a new picture source."""
        self.picture.set_paintable(paintable)

    def _on_first_frame(self, paintable):
        self.spinner.set_visible(False)
        paintable.disconnect(self._first_frame)
        self._set_ready()

    def _set_ready(self):
        if self._ready:
            return
        self._ready = True
        waiters, self._ready_waiters = self._ready_waiters, []
        for cb in waiters:
            cb()

    def when_ready(self, callback, timeout_ms):
        """callback() once the camera shows a picture, or after timeout_ms."""
        if self._ready:
            callback()
            return
        done = [False]

        def once():
            if not done[0]:
                done[0] = True
                callback()
        self._ready_waiters.append(once)
        GLib.timeout_add(timeout_ms, lambda: (once(), False)[1])

    # ---- clicks only on the picture --------------------------------------------------

    def _on_realize(self, *_):
        surface = self.get_surface()
        surface.connect("layout", lambda *_: GLib.idle_add(self._update_input_region))
        GLib.idle_add(self._update_input_region)

    def _update_input_region(self, *_):
        """Only the visible picture takes clicks: the shadow margin, the corners
        around a circle and any empty space let clicks through to the windows
        below."""
        surface = self.get_surface()
        if surface is None:
            return False
        ok, bounds = self.frame.compute_bounds(self)
        if not ok:
            return False
        x, y = bounds.get_x(), bounds.get_y()
        w, h = bounds.get_width(), bounds.get_height()
        if w < 2 or h < 2:
            return False
        # Setting the region lays the window out again: only do it on a change.
        key = (round(x), round(y), round(w), round(h), self.shape, _get("camera_radius"))
        if key == getattr(self, "_region_key", None):
            return False
        self._region_key = key
        if self.shape == "circle":
            r = min(w, h) / 2
        elif self.shape == "rounded":
            r = min(float(_get("camera_radius")), w / 2, h / 2)
        else:
            r = 0
        region = cairo.Region()
        step = 2
        top = int(y)
        while top < y + h:
            mid = top + step / 2 - y
            dy = max(0.0, r - mid, mid - (h - r))
            inset = r - math.sqrt(max(0.0, r * r - dy * dy)) if dy > 0 else 0
            region.union(cairo.RectangleInt(int(x + inset), top, max(1, int(w - 2 * inset)), step))
            top += step
        surface.set_input_region(region)
        return False

    def _on_camera_error(self, message):
        self._set_ready()
        self.spinner.set_visible(False)
        self.message_text.set_label(f"Camera unavailable\n{message}")
        self.message.set_visible(True)

    # ---- shape ------------------------------------------------------------------

    def _apply_shape(self, shape):
        if shape not in SHAPES:
            shape = "circle"
        for name in SHAPES:
            self.frame.remove_css_class(name)
        self.frame.add_css_class(shape)
        self.aspect.set_ratio(SHAPES[shape][0])
        self.shape = shape
        settings.set("camera_shape", shape)
        self.shape_button.set_icon_name(SHAPES[shape][2])
        self.shape_button.set_tooltip_text(f"Shape: {SHAPES[shape][1]}")
        self.radius_row.set_visible(shape == "rounded")
        GLib.idle_add(self._update_input_region)

    def _next_shape(self, *_):
        i = SHAPE_ORDER.index(self.shape)
        self._apply_shape(SHAPE_ORDER[(i + 1) % len(SHAPE_ORDER)])

    def _apply_radius(self, radius):
        radius = int(radius)
        self._radius_css.load_from_string(f".ck-cam-frame.rounded {{ border-radius: {radius}px; }}")
        settings.set("camera_radius", radius)
        GLib.idle_add(self._update_input_region)

    # ---- controls ------------------------------------------------------------------

    def _build_controls(self):
        box = Gtk.Box(spacing=2, css_classes=["ck-cam-controls"],
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=10)
        self.shape_button = Gtk.Button(css_classes=["flat", "circular"])
        self.shape_button.connect("clicked", self._next_shape)

        look = Gtk.MenuButton(icon_name="display-brightness-symbolic", tooltip_text="Camera settings",
                              css_classes=["flat", "circular"], popover=self._build_popover())
        mirror = Gtk.ToggleButton(icon_name="object-flip-horizontal-symbolic", tooltip_text="Mirror",
                                  css_classes=["flat", "circular"], active=_get("camera_mirror"))
        mirror.connect("toggled", self._on_mirror)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close Camera",
                           css_classes=["flat", "circular"])
        close.connect("clicked", lambda *_: self.close())
        for w in (self.shape_button, look, mirror, close):
            box.append(w)
        return box

    def _build_popover(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)

        body.append(Gtk.Label(label="Filter", xalign=0, css_classes=["heading"]))
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           max_children_per_line=3, min_children_per_line=3,
                           column_spacing=4, row_spacing=4)
        self.preset_buttons = {}
        first = None
        for key, preset in PRESETS.items():
            b = Gtk.ToggleButton(label=preset["label"])
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self._on_preset, key)
            self.preset_buttons[key] = b
            grid.append(b)
        body.append(grid)
        self.preset_hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=36,
                                     css_classes=["dim-label", "caption"])
        body.append(self.preset_hint)

        body.append(self._effects_section())
        if self.camera.has_autofocus():
            body.append(self._focus_section())
        body.append(Gtk.Label(label="Adjustments", xalign=0, css_classes=["heading"], margin_top=4))

        self.sliders = {}
        for title, lo, hi, step, key, apply in (
                ("Brightness", -50, 50, 1, "camera_brightness",
                 lambda v: self.camera.set_adjustments(brightness=v)),
                ("Contrast", 0.5, 2.0, 0.05, "camera_contrast",
                 lambda v: self.camera.set_adjustments(contrast=v)),
                ("Saturation", 0.0, 2.0, 0.05, "camera_saturation",
                 lambda v: self.camera.set_adjustments(saturation=v))):
            row = self._slider(title, lo, hi, step, key, apply)
            self.sliders[key] = row.scale
            body.append(row)
        self.radius_row = self._slider("Corner radius", 0, 120, 1, "camera_radius", self._apply_radius)
        body.append(self.radius_row)

        reset = Gtk.Button(label="Reset", css_classes=["pill"],
                           tooltip_text=f"Restore the {PRESETS[DEFAULT_PRESET]['label']} filter "
                                        "and default adjustments")
        reset.connect("clicked", lambda *_: self._select_preset(DEFAULT_PRESET))
        body.append(reset)

        current = _get("camera_look")
        current = OLD_LOOKS.get(current, current)
        self._loading = True  # mark the saved look without resetting saved sliders
        self.preset_buttons.get(current, self.preset_buttons[DEFAULT_PRESET]).set_active(True)
        self._loading = False
        self.preset_hint.set_label(PRESETS.get(current, PRESETS[DEFAULT_PRESET])["hint"])
        # Scrollable, so the menu can shrink to fit beside a small bubble near a
        # screen edge (a menu that doesn't fit is closed at once by the desktop).
        scroller = Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, propagate_natural_width=True,
                                      max_content_height=560)
        return Gtk.Popover(child=scroller)

    def _effects_section(self):
        from . import camera_fx
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=4)
        box.append(Gtk.Label(label="Effects", xalign=0, css_classes=["heading"]))
        if not camera_fx.available():
            box.append(Gtk.Label(label="Camera effects require OpenCV:\n"
                                       "sudo apt install python3-opencv",
                                 xalign=0, wrap=True, selectable=True, css_classes=["dim-label", "caption"]))
            return box
        for title, key, effect in (("Background blur", "camera_blur", "blur"),
                                   ("Skin smoothing", "camera_smooth", "smooth"),
                                   ("Face lighting", "camera_light", "light")):
            row = self._slider(title, 0, 100, 1, key,
                               lambda v, e=effect: self.camera.set_effects(**{e: v / 100}),
                               scale_factor=100)
            box.append(row)
        box.append(Gtk.Label(label="Effects are included in recordings.",
                             xalign=0, wrap=True, max_width_chars=36, css_classes=["dim-label", "caption"]))
        return box

    def _focus_section(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4)
        row = Gtk.Box(spacing=12)
        row.append(Gtk.Label(label="Lock focus", xalign=0, hexpand=True, css_classes=["heading"]))
        switch = Gtk.Switch(active=bool(_get("camera_focus_lock")), valign=Gtk.Align.CENTER)
        switch.update_property([Gtk.AccessibleProperty.LABEL], ["Lock focus"])
        switch.connect("notify::active", self._on_focus_lock)
        row.append(switch)
        box.append(row)
        box.append(Gtk.Label(label="Focuses on you, then holds it, so the camera "
                                   "doesn't refocus while you record.",
                             xalign=0, wrap=True, max_width_chars=36,
                             css_classes=["dim-label", "caption"]))
        return box

    def _on_focus_lock(self, switch, _pspec):
        settings.set("camera_focus_lock", switch.get_active())
        self.camera.set_focus_lock(switch.get_active())

    def _slider(self, title, lo, hi, step, key, apply, scale_factor=1):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(Gtk.Label(label=title, xalign=0))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_value(float(_get(key)) * scale_factor)
        scale.set_size_request(240, -1)

        def changed(s):
            v = s.get_value()
            settings.set(key, v / scale_factor)
            apply(v)

        scale.connect("value-changed", changed)
        box.append(scale)
        box.scale = scale
        return box

    def _select_preset(self, key):
        button = self.preset_buttons[key]
        if button.get_active():
            self._on_preset(button, key)  # already selected: re-apply its values
        else:
            button.set_active(True)

    def _on_preset(self, button, key):
        if not button.get_active():
            return
        preset = PRESETS[key]
        settings.set("camera_look", key)
        self.camera.set_look(key)
        self.preset_hint.set_label(preset["hint"])
        if getattr(self, "_loading", False):
            return
        # A preset also places the sliders; they stay free to fine-tune.
        self.sliders["camera_brightness"].set_value(preset["brightness"])
        self.sliders["camera_contrast"].set_value(preset["contrast"])
        self.sliders["camera_saturation"].set_value(preset["saturation"])

    def _on_mirror(self, button):
        settings.set("camera_mirror", button.get_active())
        self.camera.set_mirror(button.get_active())

    def _show_controls(self, shown):
        # A CSS class (not set_opacity) so the 150 ms fade in the CSS applies.
        if shown:
            self.controls.remove_css_class("ck-hidden")
        else:
            self.controls.add_css_class("ck-hidden")
        self.controls.set_can_target(shown)
