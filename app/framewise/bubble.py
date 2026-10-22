"""The floating camera bubble.

A small borderless window showing the webcam in a circle, a rounded
rectangle, a square or a corner tab (one big curve, facing into the screen).
Drag it anywhere; hover to reach the resize handle on the corner that faces
the middle of the screen, and its controls (shape, settings, mirror, lock focus,
take photo, close). Right-click does nothing: GNOME's window menu is kept away. It is
an ordinary window on screen, so screen and area recordings capture it
wherever it is, including when it is moved or resized mid-recording.
"""

import math
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from .camera import DEFAULT_PRESET, OLD_LOOKS, PRESETS, Camera  # noqa: E402
from .state import settings  # noqa: E402

TITLE = "framewise camera"

SHAPES = {
    # name: (aspect ratio, label, icon)
    "circle": (1.0, "Circle", "media-record-symbolic"),
    "rounded": (4 / 3, "Rounded", "view-paged-symbolic"),
    "square": (1.0, "Square", "checkbox-symbolic"),
    "corner": (1.0, "Corner", "framewise-shape-corner-symbolic"),
}
SHAPE_ORDER = ["circle", "rounded", "square", "corner"]
ICONS = Path(__file__).parent / "icons"

# The screen corner the bubble is nearest (the extension reports it), and the
# corner facing into the screen: the resize handle and the Corner shape's
# curve go there, and the window grows away from the screen edge.
OPPOSITE = {"top-left": "bottom-right", "top-right": "bottom-left",
            "bottom-left": "top-right", "bottom-right": "top-left"}
RESIZE_EDGE = {"top-left": Gdk.SurfaceEdge.NORTH_WEST, "top-right": Gdk.SurfaceEdge.NORTH_EAST,
               "bottom-left": Gdk.SurfaceEdge.SOUTH_WEST, "bottom-right": Gdk.SurfaceEdge.SOUTH_EAST}
GRIP = 26        # the resize handle's size
FRAME_MARGIN = 14  # room for the shadow around the picture (see CSS)


# Camera preview style: black surface, 14 px corners, a
# hairline white edge at 14 % opacity, a soft shadow, and controls in a
# translucent black capsule that fades in on hover (0.15 s).
CSS = """
window.ck-bubble, window.ck-bubble > * { background: transparent; box-shadow: none; }
.ck-cam-frame { background: #000; border: 1px solid rgba(255,255,255,0.14);
                box-shadow: 0 8px 24px rgba(0,0,0,0.38); margin: 14px; }
.ck-cam-frame.circle { border-radius: 9999px; }
.ck-cam-frame.square { border-radius: 0; }
.ck-cam-frame.corner { border-radius: 0; }
.ck-cam-controls { background: rgba(0,0,0,0.55); border-radius: 999px; padding: 3px;
                   transition: opacity 150ms ease-in-out; }
.ck-cam-controls.ck-hidden { opacity: 0; }
.ck-cam-controls > button, .ck-cam-controls > menubutton > button { color: white; min-width: 28px; min-height: 28px; }
.ck-cam-status { color: rgba(255,255,255,0.8); font-size: 0.85em; }
.ck-cam-grip { transition: opacity 150ms ease-in-out; }
.ck-cam-flash { background: white; opacity: 0; transition: opacity 260ms ease-out; }
.ck-cam-flash.on { opacity: 0.85; transition: none; }
.ck-cam-grip.ck-hidden { opacity: 0; }
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
        self.camera = Camera(settings.get("camera_device"), settings.get("camera_name"))
        self.camera.on_error = self._on_camera_error

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._radius_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._radius_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)

        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(ICONS))
        self.corner = "bottom-right"
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.frame = Gtk.Box(css_classes=["ck-cam-frame"], hexpand=True, vexpand=True)
        self.frame.set_overflow(Gtk.Overflow.HIDDEN)  # clip the video to the shape
        inner = Gtk.Overlay(child=self.picture, hexpand=True, vexpand=True)
        self.flash = Gtk.Box(css_classes=["ck-cam-flash"], can_target=False)
        inner.add_overlay(self.flash)
        self.grip = self._build_grip()
        inner.add_overlay(self.grip)
        self.frame.append(inner)
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
        # Right-click would open GNOME's window menu (Resize, Move...): the
        # handle does those better, so the click is taken here.
        no_menu = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        no_menu.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        no_menu.connect("pressed", lambda g, *_: g.set_state(Gtk.EventSequenceState.CLAIMED))
        self.add_controller(no_menu)

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
        self._set_corner(self.corner)

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
        # The extension places the window once it is on screen.
        GLib.timeout_add(400, self._follow_corner)

    def _shell(self):
        session = getattr(self.get_application(), "session", None)
        shell = getattr(session, "shell", None)
        return shell if shell is not None and shell.has_bubble_corner else None

    def _follow_corner(self):
        """Asks the extension where the bubble is, and to keep the window the
        picture's shape after a resize."""
        shell = self._shell()
        if shell is None:
            return False
        shell.on_bubble_corner = self._set_corner
        shell.set_bubble_aspect(SHAPES[self.shape][0], 0)
        self._set_corner(shell.bubble_corner())
        return False

    def _set_corner(self, corner):
        if corner not in OPPOSITE:
            return
        self.corner = corner
        facing = OPPOSITE[corner]
        for name in OPPOSITE:
            self.frame.remove_css_class(f"facing-{name}")
        self.frame.add_css_class(f"facing-{facing}")
        # The picture hugs the screen-edge corner while a resize is under way.
        self.aspect.set_xalign(1.0 if corner.endswith("right") else 0.0)
        self.aspect.set_yalign(1.0 if corner.startswith("bottom") else 0.0)
        self.grip.set_halign(Gtk.Align.START if facing.endswith("left") else Gtk.Align.END)
        self.grip.set_valign(Gtk.Align.START if facing.startswith("top") else Gtk.Align.END)
        self.grip.set_cursor_from_name(
            "nwse-resize" if facing in ("top-left", "bottom-right") else "nesw-resize")
        # The overlay doesn't place a child again by itself when only its alignment changes.
        self.grip.get_parent().queue_allocate()
        self.grip.queue_draw()
        GLib.idle_add(self._update_input_region)

    def _on_close(self, *_):
        settings.set("camera_size", [self.get_width(), self.get_height()])
        shell = self._shell()
        if shell is not None and shell.on_bubble_corner == self._set_corner:
            shell.on_bubble_corner = None
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
        radii = self._radii(w, h)
        self._place_grip(radii)
        bar = None
        if getattr(self, "_controls_shown", False):
            ok, b = self.controls.compute_bounds(self)
            if ok:
                bar = (int(b.get_x()), int(b.get_y()), int(b.get_width()) + 1, int(b.get_height()) + 1)
        # Setting the region lays the window out again: only do it on a change.
        key = (round(x), round(y), round(w), round(h), radii, bar)
        if key == getattr(self, "_region_key", None):
            return False
        self._region_key = key
        tl, tr, br, bl = radii
        region = cairo.Region()
        step = 2
        top = int(y)
        while top < y + h:
            mid = top + step / 2 - y
            left = max(_inset(tl, mid), _inset(bl, h - mid))
            right = max(_inset(tr, mid), _inset(br, h - mid))
            region.union(cairo.RectangleInt(int(x + left), top, max(1, int(w - left - right)), step))
            top += step
        if bar:
            region.union(cairo.RectangleInt(*bar))
        surface.set_input_region(region)
        return False

    def _radii(self, w, h):
        """Corner radii of the picture: top-left, top-right, bottom-right, bottom-left."""
        if self.shape == "circle":
            r = min(w, h) / 2
            return (r, r, r, r)
        if self.shape == "square":
            return (0, 0, 0, 0)
        r = min(float(_get("camera_radius")), w / 2, h / 2)
        if self.shape == "rounded":
            return (r, r, r, r)
        facing = OPPOSITE[self.corner]
        return tuple(r if name == facing else 0
                     for name in ("top-left", "top-right", "bottom-right", "bottom-left"))

    # ---- resize handle --------------------------------------------------------------

    def _build_grip(self):
        grip = Gtk.DrawingArea(content_width=GRIP, content_height=GRIP, css_classes=["ck-cam-grip"],
                               tooltip_text="Drag to resize")
        grip.set_draw_func(self._draw_grip)
        press = Gtk.GestureClick(button=1)
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", self._on_grip_pressed)
        grip.add_controller(press)
        return grip

    def _draw_grip(self, _area, cr, width, height):
        """A curved bracket in the corner, like the corner of the picture."""
        facing = OPPOSITE[self.corner]
        cr.save()
        # Drawn for the top-left corner, flipped for the others.
        if facing.endswith("right"):
            cr.translate(width, 0)
            cr.scale(-1, 1)
        if facing.startswith("bottom"):
            cr.translate(0, height)
            cr.scale(1, -1)
        pad, size = 5, min(width, height) - 9
        cr.arc(pad + size, pad + size, size, math.pi, 1.5 * math.pi)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.set_line_width(6)
        cr.stroke_preserve()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(3)
        cr.stroke()
        cr.restore()

    def _place_grip(self, radii):
        """Keeps the handle inside the shape, on its curve."""
        facing = OPPOSITE[self.corner]
        r = radii[("top-left", "top-right", "bottom-right", "bottom-left").index(facing)]
        inset = max(4, int(r * (1 - math.sqrt(0.5))) - 2)
        if (inset, facing) != getattr(self, "_grip_key", None):
            self._grip_key = (inset, facing)
            for side in ("top", "bottom", "start", "end"):
                getattr(self.grip, f"set_margin_{side}")(inset)
            self.grip.get_parent().queue_allocate()

    def _on_grip_pressed(self, gesture, _n, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        surface = self.get_surface()
        ok, point = self.grip.compute_point(self, Graphene.Point().init(x, y))
        if surface is None or not ok:
            return
        sx, sy = self.get_surface_transform()
        # GNOME does the resize, keeping the opposite corner (at the screen
        # edge) in place; the extension then trims the window to the picture.
        surface.begin_resize(RESIZE_EDGE[OPPOSITE[self.corner]], gesture.get_device(),
                             gesture.get_current_button(), point.x + sx, point.y + sy,
                             gesture.get_current_event_time())

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
        self.radius_row.set_visible(shape in ("rounded", "corner"))
        shell = self._shell() if self.get_realized() else None
        if shell is not None:
            shell.set_bubble_aspect(SHAPES[shape][0], 0)
        GLib.idle_add(self._update_input_region)

    def _next_shape(self, *_):
        i = SHAPE_ORDER.index(self.shape)
        self._apply_shape(SHAPE_ORDER[(i + 1) % len(SHAPE_ORDER)])

    def _apply_radius(self, radius):
        radius = int(radius)
        css = f".ck-cam-frame.rounded {{ border-radius: {radius}px; }}\n"
        for facing, corners in (("top-left", f"{radius}px 0 0 0"), ("top-right", f"0 {radius}px 0 0"),
                                ("bottom-right", f"0 0 {radius}px 0"),
                                ("bottom-left", f"0 0 0 {radius}px")):
            css += f".ck-cam-frame.corner.facing-{facing} {{ border-radius: {corners}; }}\n"
        self._radius_css.load_from_string(css)
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
        buttons = [self.shape_button, look, mirror]
        if self.camera.has_autofocus():
            focus = Gtk.ToggleButton(icon_name="framewise-focus-lock-symbolic", tooltip_text="Lock Focus",
                                     css_classes=["flat", "circular"],
                                     active=bool(_get("camera_focus_lock")))
            focus.connect("toggled", self._on_focus_lock)
            buttons.append(focus)
        photo = Gtk.Button(icon_name="camera-photo-symbolic", tooltip_text="Take Photo",
                           css_classes=["flat", "circular"])
        photo.connect("clicked", self._take_photo)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close Camera",
                           css_classes=["flat", "circular"])
        close.connect("clicked", lambda *_: self.close())
        for w in buttons + [photo, close]:
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

    def _on_focus_lock(self, button):
        """Locked: the camera focuses on you, then holds it, so it doesn't
        refocus while you record."""
        settings.set("camera_focus_lock", button.get_active())
        self.camera.set_focus_lock(button.get_active())

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

    # ---- photo ------------------------------------------------------------------

    def _current_frame(self):
        """The picture as shown (filters, effects, mirror), at camera size."""
        paintable = self.camera.paintable
        if paintable is None:
            return None
        image = paintable.get_current_image()
        if isinstance(image, Gdk.Texture):
            return image
        w, h = image.get_intrinsic_width(), image.get_intrinsic_height()
        if w <= 0 or h <= 0:
            return None
        snap = Gtk.Snapshot()
        image.snapshot(snap, w, h)
        node = snap.to_node()
        return self.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h)) if node else None

    def _take_photo(self, *_):
        """Saved straight to Pictures; a flash and the shutter sound say it was
        taken (no sound while recording: it would be in the video)."""
        from .shots import shot_destination
        frame = self._current_frame()
        if frame is None:
            return
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        path = shot_destination(pictures or str(Path.home() / "Pictures"), prefix="Photo")
        self._flash()
        session = getattr(self.get_application(), "session", None)
        if session is None or session.phase == session.IDLE:
            from . import sounds
            sounds.play("camera-shutter")
        try:
            frame.save_to_png(str(path))
        except GLib.Error as e:
            self.get_application().notify("The photo could not be saved", e.message)

    def _flash(self):
        self.flash.add_css_class("on")
        GLib.timeout_add(120, lambda: self.flash.remove_css_class("on") or False)

    def _on_mirror(self, button):
        settings.set("camera_mirror", button.get_active())
        self.camera.set_mirror(button.get_active())

    def _show_controls(self, shown):
        # A CSS class (not set_opacity) so the 150 ms fade in the CSS applies.
        self._controls_shown = shown
        GLib.idle_add(self._update_input_region)
        for widget in (self.controls, self.grip):
            if shown:
                widget.remove_css_class("ck-hidden")
            else:
                widget.add_css_class("ck-hidden")
            widget.set_can_target(shown)


def _inset(r, d):
    """How far a corner of radius r cuts in, d pixels from that edge."""
    if r <= 0 or d >= r:
        return 0
    dy = r - d
    return r - math.sqrt(max(0.0, r * r - dy * dy))
