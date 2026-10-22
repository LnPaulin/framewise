"""The screenshot editor: arrows, boxes, text, numbers, blur, crop, background.

Opens from the preview card's Edit button (or a pinned screenshot). Save
writes the marked-up picture over the screenshot file; Save as makes a new
one; Copy puts it on the clipboard. Every change can be undone.
"""

import copy
import json
import math
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from . import compose  # noqa: E402
from . import shot_draw as sd  # noqa: E402
from .state import settings  # noqa: E402

# (tool, label, key, default colour)
TOOLS = [
    ("select", "Select", "v", None),
    ("arrow", "Arrow", "a", "red"),
    ("line", "Line", "l", "red"),
    ("rect", "Rectangle", "r", "red"),
    ("ellipse", "Ellipse", "o", "red"),
    ("pen", "Pen", "p", "red"),
    ("highlighter", "Highlighter", "h", "yellow"),
    ("text", "Text", "t", "red"),
    ("number", "Counter", "n", "red"),
    ("blur", "Blur", "b", None),
    ("pixelate", "Pixelate", "x", None),
    ("redact", "Redact", "d", "black"),
    ("spotlight", "Spotlight", "s", None),
    ("crop", "Crop", "c", None),
]
TOOL_KEYS = {key: tool for tool, _l, key, _c in TOOLS}
SWATCHES = ["red", "orange", "yellow", "green", "blue", "purple", "white", "black"]
HANDLE = 5            # handle half-size, view pixels
MIN_DRAG = 4          # view pixels a drag must cover to make a mark

CSS = """
.ck-shot-stage { background-color: #2b2b30; }
.ck-shot-swatch { min-width: 22px; min-height: 22px; padding: 0; border-radius: 99px;
                  border: 2px solid alpha(white, 0.0); }
.ck-shot-swatch.chosen { border-color: @accent_color; }
.ck-shot-tools button { padding: 6px; }
.ck-shot-entry { background: alpha(black, 0.35); color: white; font-weight: bold;
                 border-radius: 6px; }
""" + "".join(
    f".ck-shot-swatch-{n} {{ background: rgb({round(r * 255)},{round(g * 255)},{round(b * 255)}); }}\n"
    for n, (r, g, b) in sd.COLORS.items()) + "".join(
    f".ck-shot-bg-{name} {{ background: linear-gradient(135deg, "
    f"rgb({round(a[0] * 255)},{round(a[1] * 255)},{round(a[2] * 255)}), "
    f"rgb({round(b[0] * 255)},{round(b[1] * 255)},{round(b[2] * 255)})); border-radius: 7px; }}\n"
    for name, (a, b) in compose.PRESETS.items())


def _draw_tool_icon(tool, cr, w, h, color):
    """Small line drawings for the toolbar, so they look the same on any icon theme."""
    cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
    cr.set_line_width(1.7)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    s = min(w, h)
    cr.translate((w - s) / 2, (h - s) / 2)
    cr.scale(s / 20, s / 20)
    if tool == "select":
        cr.move_to(5, 3); cr.line_to(5, 16); cr.line_to(8.5, 12.5); cr.line_to(11, 17.5)
        cr.line_to(13, 16.5); cr.line_to(10.5, 11.5); cr.line_to(15, 11.5); cr.close_path(); cr.stroke()
    elif tool == "arrow":
        cr.move_to(4, 16); cr.line_to(15, 5); cr.stroke()
        cr.move_to(16, 4); cr.line_to(9, 5.5); cr.line_to(14.5, 11); cr.close_path(); cr.fill()
    elif tool == "line":
        cr.move_to(4, 16); cr.line_to(16, 4); cr.stroke()
    elif tool == "rect":
        sd._rounded(cr, 3.5, 5, 13, 10, 1.5); cr.stroke()
    elif tool == "ellipse":
        cr.save(); cr.translate(10, 10); cr.scale(7, 5.5); cr.arc(0, 0, 1, 0, 2 * math.pi); cr.restore(); cr.stroke()
    elif tool == "pen":
        cr.move_to(3.5, 14); cr.curve_to(6, 6, 9, 16, 11, 9); cr.curve_to(12.5, 4, 15, 8, 16.5, 5); cr.stroke()
    elif tool == "highlighter":
        cr.set_line_width(5); cr.set_source_rgba(color.red, color.green, color.blue, 0.45)
        cr.move_to(3.5, 13); cr.line_to(16.5, 13); cr.stroke()
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha); cr.set_line_width(1.7)
        cr.move_to(9, 10); cr.line_to(14, 3.5); cr.line_to(16.5, 5.5); cr.line_to(11.5, 12); cr.close_path(); cr.stroke()
    elif tool == "text":
        cr.move_to(4.5, 5); cr.line_to(15.5, 5); cr.move_to(10, 5); cr.line_to(10, 16); cr.stroke()
    elif tool == "number":
        cr.arc(10, 10, 7, 0, 2 * math.pi); cr.stroke()
        cr.move_to(8.5, 7.5); cr.line_to(10.5, 6); cr.line_to(10.5, 14); cr.stroke()
    elif tool == "blur":
        for i, a in enumerate((0.25, 0.5, 0.8)):
            cr.set_source_rgba(color.red, color.green, color.blue, a)
            cr.arc(5 + i * 5, 10, 3.2, 0, 2 * math.pi); cr.fill()
    elif tool == "pixelate":
        for i in range(3):
            for j in range(3):
                if (i + j) % 2 == 0:
                    cr.rectangle(4 + i * 4, 4 + j * 4, 4, 4)
        cr.fill(); cr.rectangle(4, 4, 12, 12); cr.stroke()
    elif tool == "redact":
        sd._rounded(cr, 3, 6, 14, 8, 1.5); cr.fill()
    elif tool == "spotlight":
        cr.rectangle(2.5, 3.5, 15, 13); cr.stroke()
        cr.arc(10, 10, 3.5, 0, 2 * math.pi); cr.fill()
    elif tool == "crop":
        cr.move_to(6, 2.5); cr.line_to(6, 14); cr.line_to(17.5, 14)
        cr.move_to(2.5, 6); cr.line_to(14, 6); cr.line_to(14, 17.5); cr.stroke()


def _rect(x, y, w, h):
    r = Graphene.Rect()
    r.init(x, y, w, h)
    return r


def surface_texture(surface):
    """A cairo picture as a texture the graphics card can draw."""
    surface.flush()
    return Gdk.MemoryTexture.new(
        surface.get_width(), surface.get_height(), Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
        GLib.Bytes.new(bytes(surface.get_data())), surface.get_stride())


class Canvas(Gtk.Widget):
    """The picture area. The picture and background are drawn by the graphics
    card from cached textures; only the marks are drawn with cairo."""

    def __init__(self, editor):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.editor = editor

    def do_snapshot(self, snapshot):
        self.editor._snapshot(snapshot)


class ShotEditor(Adw.ApplicationWindow):
    def __init__(self, app, path, on_closed=None, on_pin=None):
        super().__init__(application=app, title="Screenshot", default_width=1200, default_height=800)
        self.path = Path(path)
        self.on_closed = on_closed
        self.on_pin = on_pin
        self.base = cairo.ImageSurface.create_from_png(str(self.path))
        self.img_w, self.img_h = self.base.get_width(), self.base.get_height()
        self.u = sd.unit(self.img_w, self.img_h)
        self.doc = sd.new_doc()
        bg_saved = settings.get("shot_bg")
        if isinstance(bg_saved, dict):
            self.doc["bg"].update({k: v for k, v in bg_saved.items() if k != "kind"})
        self.saved_doc = copy.deepcopy(self.doc)
        self.undo_stack, self.redo_stack = [], []
        self.tool = "arrow"
        self.colors = {t: c for t, _l, _k, c in TOOLS if c}
        self.size = settings.get("shot_size") or "m"
        self.selected = None          # index into doc["items"]
        self.action = None            # what the current drag does
        self.editing_text = None
        self._flat_cache = (None, None)       # (regions, texture)
        self._backdrop_cache = (None, None)   # (key, texture)

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build()
        self._set_tool("arrow")
        self.connect("close-request", self._on_close_request)

    # ---- layout -------------------------------------------------------------------------

    def _build(self):
        header = Adw.HeaderBar()
        self.undo_btn = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Undo (Ctrl+Z)")
        self.undo_btn.connect("clicked", lambda *_: self.undo())
        self.redo_btn = Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text="Redo (Ctrl+Shift+Z)")
        self.redo_btn.connect("clicked", lambda *_: self.redo())
        header.pack_start(self.undo_btn)
        header.pack_start(self.redo_btn)
        self.title = Adw.WindowTitle(title="Screenshot", subtitle=self.path.name)
        header.set_title_widget(self.title)

        save = Gtk.Button(label="Save", css_classes=["suggested-action"], tooltip_text="Save (Ctrl+S)")
        save.connect("clicked", lambda *_: self.save())
        copy_btn = Gtk.Button(label="Copy", tooltip_text="Copy image (Ctrl+C)")
        copy_btn.connect("clicked", lambda *_: self.copy())
        self.text_btn = Gtk.Button(label="Copy Text", tooltip_text="Copy text from the image (Ctrl+Shift+C)")
        self.text_btn.connect("clicked", lambda *_: self.copy_text())
        menu = Gio.Menu()
        menu.append("Save As…", "win.save-as")
        menu.append("Pin to Screen", "win.pin")
        menu.append("Show in Files", "win.show-folder")
        menu.append("Delete Screenshot", "win.delete")
        menu.append("Settings", "app.preferences")
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="More")
        header.pack_end(more)
        header.pack_end(save)
        header.pack_end(copy_btn)
        header.pack_end(self.text_btn)
        for name, cb in (("save-as", self.save_as), ("pin", self.pin), ("show-folder", self._show_folder),
                         ("delete", self.delete)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(action)

        # Tools down the left side.
        tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=6, margin_end=6,
                        margin_top=8, css_classes=["ck-shot-tools"])
        self.tool_buttons = {}
        first = None
        for tool, label, key, _c in TOOLS:
            icon = Gtk.DrawingArea(content_width=20, content_height=20)
            icon.set_draw_func(lambda area, cr, w, h, t=tool: _draw_tool_icon(t, cr, w, h, area.get_color()))
            b = Gtk.ToggleButton(child=icon, css_classes=["flat"], tooltip_text=f"{label}  ({key.upper()})",
                                 group=first)
            first = first or b
            b.connect("toggled", lambda b, t=tool: b.get_active() and self._set_tool(t))
            tools.append(b)
            self.tool_buttons[tool] = b

        # Options for the tool, above the picture.
        self.options = Gtk.Box(spacing=10, margin_start=10, margin_end=10, margin_top=6, margin_bottom=6)
        self.swatch_box = Gtk.Box(spacing=4)
        self.swatches = {}
        for name in SWATCHES:
            b = Gtk.Button(css_classes=["ck-shot-swatch", f"ck-shot-swatch-{name}"], tooltip_text=name.capitalize())
            b.connect("clicked", lambda _b, n=name: self._set_color(n))
            self.swatch_box.append(b)
            self.swatches[name] = b
        self.options.append(self.swatch_box)
        self.size_box = Gtk.Box(css_classes=["linked"])
        self.size_buttons = {}
        first = None
        for key, label in (("s", "S"), ("m", "M"), ("l", "L")):
            b = Gtk.ToggleButton(label=label, group=first, tooltip_text={"s": "Thin", "m": "Medium",
                                                                         "l": "Thick"}[key])
            first = first or b
            b.connect("toggled", lambda b, k=key: b.get_active() and self._set_size(k))
            self.size_box.append(b)
            self.size_buttons[key] = b
        self.options.append(self.size_box)
        self.fill_btn = Gtk.ToggleButton(label="Fill", tooltip_text="Fill the shape")
        self.fill_btn.connect("toggled", lambda b: self._set_fill(b.get_active()))
        self.options.append(self.fill_btn)
        self.crop_reset = Gtk.Button(label="Reset Crop")
        self.crop_reset.connect("clicked", lambda *_: self._reset_crop())
        self.crop_done = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        self.crop_done.connect("clicked", lambda *_: self.tool_buttons["select"].set_active(True))
        self.options.append(self.crop_reset)
        self.options.append(self.crop_done)
        self.hint = Gtk.Label(css_classes=["dim-label"], hexpand=True, xalign=0)
        self.options.append(self.hint)
        self.delete_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete annotation (Delete)",
                                     css_classes=["flat"])
        self.delete_btn.connect("clicked", lambda *_: self._delete_selected())
        self.options.append(self.delete_btn)
        self.options.append(self._build_background_button())

        # The picture.
        self.canvas = Canvas(self)
        self.canvas.add_css_class("ck-shot-stage")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.canvas.add_controller(drag)
        clicks = Gtk.GestureClick()
        clicks.connect("pressed", self._clicked)
        self.canvas.add_controller(clicks)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._hover)
        self.canvas.add_controller(motion)
        self.text_entry = Gtk.Entry(halign=Gtk.Align.START, valign=Gtk.Align.START, visible=False,
                                    css_classes=["ck-shot-entry"], width_chars=12)
        self.text_entry.connect("activate", lambda *_: self._finish_text())
        self.text_entry.connect("changed", lambda *_: self._text_changed())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._finish_text())
        self.text_entry.add_controller(focus)
        stage = Gtk.Overlay(child=self.canvas)
        stage.add_overlay(self.text_entry)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right.append(self.options)
        right.append(stage)
        body = Gtk.Box()
        body.append(tools)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(right)
        self.toasts = Adw.ToastOverlay(child=body)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_content(view)
        # Open at a comfortable size for the picture.
        k = min(1.0, 1400 / self.img_w, 850 / self.img_h)
        self.set_default_size(max(760, round(self.img_w * k) + 120), max(520, round(self.img_h * k) + 150))

    def _build_background_button(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                       margin_bottom=12, margin_start=12, margin_end=12, width_request=280)
        body.append(Gtk.Label(label="Background", xalign=0, css_classes=["heading"]))
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=7,
                          min_children_per_line=4, column_spacing=6, row_spacing=6)
        none = Gtk.Button(label="None")
        none.connect("clicked", lambda *_: self._set_bg(kind="none"))
        row.append(none)
        for name in compose.PRESETS:
            b = Gtk.Button(css_classes=[f"ck-shot-bg-{name}"], tooltip_text=name.capitalize())
            b.set_size_request(34, 26)
            b.connect("clicked", lambda _b, n=name: self._set_bg(kind="preset", preset=n))
            row.append(b)
        dark = Gtk.Button(label="Charcoal")
        dark.connect("clicked", lambda *_: self._set_bg(kind="color", color=[0.12, 0.12, 0.14]))
        row.append(dark)
        body.append(row)
        self.bg_scales = {}
        for key, title in (("padding", "Padding"), ("radius", "Corner radius")):
            body.append(Gtk.Label(label=title, xalign=0))
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
            scale.set_draw_value(False)
            scale.set_value(self.doc["bg"][key])
            scale.connect("value-changed", lambda s, k=key: self._set_bg(merge=True, **{k: s.get_value()}))
            body.append(scale)
            self.bg_scales[key] = scale
        self.shadow_switch = Adw.SwitchRow(title="Shadow", active=self.doc["bg"]["shadow"])
        self.shadow_switch.connect("notify::active", lambda r, _p: self._set_bg(shadow=r.get_active()))
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(self.shadow_switch)
        body.append(box)
        self._update_bg_controls()
        button = Gtk.MenuButton(popover=Gtk.Popover(child=body), tooltip_text="Background")
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image(icon_name="image-x-generic-symbolic"))
        content.append(Gtk.Label(label="Background"))
        button.set_child(content)
        return button

    # ---- view geometry ------------------------------------------------------------------

    def _view(self):
        """(scale, offset_x, offset_y, image_x, image_y, canvas_w, canvas_h)."""
        editing_crop = self.tool == "crop"
        W, H, ox, oy, _crop = sd.output_layout(self.doc, self.img_w, self.img_h, editing_crop)
        aw, ah = max(1, self.canvas.get_width()), max(1, self.canvas.get_height())
        margin = 24
        s = min((aw - 2 * margin) / W, (ah - 2 * margin) / H, 1.0)
        s = max(s, 0.05)
        return s, (aw - W * s) / 2, (ah - H * s) / 2, ox, oy, W, H

    def to_image(self, vx, vy):
        s, offx, offy, ox, oy, *_ = self._view()
        return ((vx - offx) / s - ox, (vy - offy) / s - oy)

    def to_view(self, ix, iy):
        s, offx, offy, ox, oy, *_ = self._view()
        return (offx + (ix + ox) * s, offy + (iy + oy) * s)

    # ---- drawing ------------------------------------------------------------------------

    def _flat_texture(self):
        regions = [it for it in self.doc["items"] if it["type"] in sd.REGION_TYPES]
        key = json.dumps(regions)
        if self._flat_cache[0] != key:
            self._flat_cache = (key, surface_texture(sd.flatten(self.base, regions)))
        return self._flat_cache[1]

    def _backdrop_texture(self, W, H, s):
        """The background and shadow, drawn once at the size they're shown."""
        k = s * self.canvas.get_scale_factor()
        tw, th = max(1, round(W * k)), max(1, round(H * k))
        key = (json.dumps(self.doc["bg"]), json.dumps(self.doc["crop"]), tw, th)
        if self._backdrop_cache[0] != key:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, tw, th)
            cr = cairo.Context(surface)
            cr.scale(tw / W, th / H)
            sd.paint_backdrop(cr, self.doc, self.img_w, self.img_h)
            self._backdrop_cache = (key, surface_texture(surface))
        return self._backdrop_cache[1]

    def _snapshot(self, snap):
        aw, ah = self.canvas.get_width(), self.canvas.get_height()
        if aw < 2 or ah < 2:
            return
        editing_crop = self.tool == "crop"
        s, offx, offy, ox, oy, W, H = self._view()
        _W, _H, _ox, _oy, (cx, cy, cw, ch) = sd.output_layout(self.doc, self.img_w, self.img_h, editing_crop)
        skip = self.doc["items"][self.editing_text] if self.editing_text is not None else None
        snap.save()
        point = Graphene.Point()
        point.init(offx, offy)
        snap.translate(point)
        snap.scale(s, s)
        if not editing_crop and self.doc["bg"]["kind"] != "none":
            snap.append_texture(self._backdrop_texture(W, H, s), _rect(0, 0, W, H))
        clip = Gsk.RoundedRect()
        clip.init_from_rect(_rect(ox + cx, oy + cy, cw, ch),
                            sd.backdrop_radius(self.doc, self.img_w, self.img_h, editing_crop))
        snap.push_rounded_clip(clip)
        # Smooth when shrunk; sharp pixels when shown at full size.
        filt = Gsk.ScalingFilter.TRILINEAR if s < 0.999 else Gsk.ScalingFilter.NEAREST
        snap.append_scaled_texture(self._flat_texture(), filt, _rect(ox, oy, self.img_w, self.img_h))
        if sd.has_marks(self.doc, skip):
            cr = snap.append_cairo(_rect(ox + cx, oy + cy, cw, ch))
            cr.translate(ox, oy)
            sd.paint_marks(cr, self.doc, W, H, ox, oy, skip)
        snap.pop()
        snap.restore()
        overlay = editing_crop or (self.action and self.action.get("preview")) or (
            self.selected is not None and self.selected < len(self.doc["items"]))
        if overlay:
            self._draw_overlay(snap.append_cairo(_rect(0, 0, aw, ah)), aw, ah, s)

    def _draw_overlay(self, cr, aw, ah, s):
        if self.tool == "crop":
            self._draw_crop(cr, aw, ah)
            return
        if self.action and self.action.get("preview"):
            # A blur or block being drawn: its outline until the button is released.
            x, y, w, h = sd.norm_box(self.action["preview"])
            x0, y0 = self.to_view(x, y)
            cr.rectangle(x0, y0, w * s, h * s)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(1.5)
            cr.set_dash([5, 4])
            cr.stroke()
            cr.set_dash([])
        if self.selected is not None and self.selected < len(self.doc["items"]):
            self._draw_selection(cr, self.doc["items"][self.selected], s)

    def _draw_selection(self, cr, item, s):
        x, y, w, h = sd.item_bounds(item)
        pad = 6 / s
        x0, y0 = self.to_view(x - pad, y - pad)
        cr.rectangle(x0, y0, (w + 2 * pad) * s, (h + 2 * pad) * s)
        cr.set_source_rgba(0.21, 0.52, 0.89, 0.9)
        cr.set_line_width(1.2)
        cr.set_dash([4, 3])
        cr.stroke()
        cr.set_dash([])
        for _name, hx, hy in sd.handles(item):
            vx, vy = self.to_view(hx, hy)
            cr.rectangle(vx - HANDLE, vy - HANDLE, 2 * HANDLE, 2 * HANDLE)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.set_line_width(1.5)
            cr.stroke()

    def _draw_crop(self, cr, aw, ah):
        cx, cy, cw, ch = sd.crop_rect(self.doc, self.img_w, self.img_h)
        s = self._view()[0]
        x0, y0 = self.to_view(0, 0)
        x1, y1 = self.to_view(self.img_w, self.img_h)
        vx, vy = self.to_view(cx, cy)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        cr.rectangle(vx, vy, cw * s, ch * s)
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.fill()
        cr.rectangle(vx, vy, cw * s, ch * s)
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(1.5)
        cr.stroke()
        # Rule-of-thirds guides.
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(1)
        for i in (1, 2):
            cr.move_to(vx + cw * s * i / 3, vy)
            cr.line_to(vx + cw * s * i / 3, vy + ch * s)
            cr.move_to(vx, vy + ch * s * i / 3)
            cr.line_to(vx + cw * s, vy + ch * s * i / 3)
        cr.stroke()
        label = f"{cw} × {ch}"
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(12)
        ext = cr.text_extents(label)
        bx, by = vx, min(ah - 24, vy + ch * s + 8)
        sd._rounded(cr, bx, by, ext.x_advance + 16, 22, 6)
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.fill()
        cr.move_to(bx + 8, by + 15)
        cr.set_source_rgb(1, 1, 1)
        cr.show_text(label)

    # ---- tools and options --------------------------------------------------------------

    def _set_tool(self, tool):
        self._finish_text()
        self.tool = tool
        if not self.tool_buttons[tool].get_active():
            self.tool_buttons[tool].set_active(True)
        if tool != "select":
            self.selected = None
        self._update_options()
        cursor = "default" if tool == "select" else ("text" if tool == "text" else "crosshair")
        self.canvas.set_cursor(Gdk.Cursor.new_from_name(cursor))
        self.canvas.queue_draw()

    def _options_for(self):
        """The tool whose options show: the selected mark's, else the current tool's."""
        if self.selected is not None and self.selected < len(self.doc["items"]):
            return self.doc["items"][self.selected]["type"], self.doc["items"][self.selected]
        return self.tool, None

    def _update_options(self):
        kind, item = self._options_for()
        colored = kind in self.colors
        self.swatch_box.set_visible(colored)
        self.size_box.set_visible(kind in ("arrow", "line", "rect", "ellipse", "pen", "highlighter",
                                           "text", "number", "blur", "pixelate"))
        self.fill_btn.set_visible(kind in ("rect", "ellipse"))
        self.crop_reset.set_visible(kind == "crop")
        self.crop_done.set_visible(kind == "crop")
        self.delete_btn.set_visible(item is not None)
        color = item.get("color") if item else self.colors.get(kind)
        for name, b in self.swatches.items():
            if name == color:
                b.add_css_class("chosen")
            else:
                b.remove_css_class("chosen")
        self._quiet = True
        size = item.get("size_key", self.size) if item else self.size
        self.size_buttons[size].set_active(True)
        self.fill_btn.set_active(bool(item and item.get("fill")))
        self._quiet = False
        self.hint.set_label({
            "select": "Click an annotation to edit or move it",
            "text": "Click where the text goes",
            "number": "Click to place the next counter",
            "crop": "Drag to select the area to keep",
            "spotlight": "Drag over the area to highlight; the rest is dimmed",
            "blur": "Drag over the area to blur", "pixelate": "Drag over the area to pixelate",
            "redact": "Drag over the area to redact",
        }.get(kind, ""))
        self.undo_btn.set_sensitive(bool(self.undo_stack))
        self.redo_btn.set_sensitive(bool(self.redo_stack))

    def _set_color(self, name):
        kind, item = self._options_for()
        if item is not None:
            self._change(lambda d: d["items"][self.selected].update(color=name))
            self.colors[item["type"]] = name
        elif kind in self.colors:
            self.colors[kind] = name
        self._update_options()

    def _set_size(self, key):
        if getattr(self, "_quiet", False):
            return
        self.size = key
        settings.set("shot_size", key)
        kind, item = self._options_for()
        if item is not None:
            self._change(lambda d: self._apply_size(d["items"][self.selected], key))

    def _apply_size(self, item, key):
        item["size_key"] = key
        if item["type"] in ("text", "number"):
            item["size"] = sd.TEXT_SIZES[key] * self.u
        elif item["type"] in ("blur", "pixelate"):
            item["strength"] = {"s": 2, "m": 3, "l": 5}[key]
        elif item["type"] in sd.LINE_TYPES + sd.PATH_TYPES + ("rect", "ellipse"):
            item["width"] = sd.WIDTHS[key] * self.u

    def _set_fill(self, on):
        if getattr(self, "_quiet", False):
            return
        self._fill_default = on
        if self.selected is not None:
            self._change(lambda d: d["items"][self.selected].update(fill=on))

    def _set_bg(self, merge=False, **changes):
        def apply(d):
            d["bg"].update(changes)
        self._change(apply, merge="bg" if merge else None)
        settings.set("shot_bg", self.doc["bg"])
        self._update_bg_controls()

    def _update_bg_controls(self):
        """Padding and shadow need a background; corner radius works without one."""
        has_bg = self.doc["bg"]["kind"] != "none"
        self.bg_scales["padding"].set_sensitive(has_bg)
        self.shadow_switch.set_sensitive(has_bg)

    def _reset_crop(self):
        self._change(lambda d: d.update(crop=None))

    # ---- changes and undo ---------------------------------------------------------------

    def _change(self, fn, merge=None):
        """Applies fn to a copy of the document as one undo step."""
        before = copy.deepcopy(self.doc)
        fn(self.doc)
        if self.doc == before:
            return
        if not (merge and self._last_merge == merge):
            self.undo_stack.append(before)
            self.redo_stack.clear()
        self._last_merge = merge
        self._changed()

    _last_merge = None

    def _changed(self):
        dirty = self.doc != self.saved_doc
        self.title.set_title("Screenshot" + (" •" if dirty else ""))
        self._update_options()
        self.canvas.queue_draw()

    def undo(self):
        self._finish_text()
        if self.undo_stack:
            self.redo_stack.append(copy.deepcopy(self.doc))
            self.doc = self.undo_stack.pop()
            self.selected = None
            self._last_merge = None
            self._changed()

    def redo(self):
        self._finish_text()
        if self.redo_stack:
            self.undo_stack.append(copy.deepcopy(self.doc))
            self.doc = self.redo_stack.pop()
            self.selected = None
            self._last_merge = None
            self._changed()

    def _delete_selected(self):
        if self.selected is None:
            return
        index, self.selected = self.selected, None
        self._change(lambda d: d["items"].pop(index))

    # ---- mouse --------------------------------------------------------------------------

    def _new_item(self, kind, x, y):
        color = self.colors.get(kind, "red")
        item = {"type": kind, "color": color, "size_key": self.size}
        if kind in sd.LINE_TYPES:
            item["p"] = [x, y, x, y]
        elif kind in sd.PATH_TYPES:
            item["pts"] = [x, y]
        elif kind in sd.BOX_TYPES:
            item["r"] = [x, y, 0, 0]
            if kind in ("rect", "ellipse"):
                item["fill"] = bool(getattr(self, "_fill_default", False))
        self._apply_size(item, self.size)
        return item

    def _handle_at(self, vx, vy):
        if self.selected is None or self.selected >= len(self.doc["items"]):
            return None
        for name, hx, hy in sd.handles(self.doc["items"][self.selected]):
            px, py = self.to_view(hx, hy)
            if abs(px - vx) <= HANDLE + 3 and abs(py - vy) <= HANDLE + 3:
                return name
        return None

    def _item_at(self, x, y):
        tol = 6 / self._view()[0]
        items = self.doc["items"]
        # Marks drawn on top win over the areas under them (blur, spotlight…).
        under = sd.REGION_TYPES + ("spotlight",)
        for areas in (False, True):
            for i in range(len(items) - 1, -1, -1):
                if (items[i]["type"] in under) == areas and sd.hit(items[i], x, y, tol):
                    return i
        return None

    def _clicked(self, gesture, n_press, vx, vy):
        self.canvas.grab_focus()
        x, y = self.to_image(vx, vy)
        if n_press == 2 and self.tool == "select":
            i = self._item_at(x, y)
            if i is not None and self.doc["items"][i]["type"] == "text":
                self._edit_text(i)

    def _drag_begin(self, gesture, vx, vy):
        self._finish_text()
        x, y = self.to_image(vx, vy)
        tool = self.tool
        self.action = None
        if tool == "crop":
            self.action = {"kind": "crop", "start": (x, y), "before": copy.deepcopy(self.doc)}
            return
        # Any tool can grab the selected mark's handles.
        handle = self._handle_at(vx, vy)
        if handle:
            self.action = {"kind": "handle", "name": handle, "before": copy.deepcopy(self.doc)}
            return
        if tool == "select":
            i = self._item_at(x, y)
            self.selected = i
            if i is not None:
                self.action = {"kind": "move", "last": (x, y), "before": copy.deepcopy(self.doc)}
            self._update_options()
            self.canvas.queue_draw()
            return
        if tool == "text":
            i = self._item_at(x, y)
            if i is not None and self.doc["items"][i]["type"] == "text":
                self._edit_text(i)
                return
            before = copy.deepcopy(self.doc)
            item = self._new_item("text", x, y)
            item.update(text="", p=[x, y - item["size"] * 0.6])
            self.doc["items"].append(item)
            self.undo_stack.append(before)
            self.redo_stack.clear()
            self._edit_text(len(self.doc["items"]) - 1, new=True)
            return
        if tool == "number":
            before = copy.deepcopy(self.doc)
            item = self._new_item("number", x, y)
            item["p"] = [x, y]
            item["n"] = 1 + max([it["n"] for it in self.doc["items"] if it["type"] == "number"] or [0])
            self.doc["items"].append(item)
            self.undo_stack.append(before)
            self.redo_stack.clear()
            self._last_merge = None
            self.selected = len(self.doc["items"]) - 1
            self._changed()
            return
        item = self._new_item(tool, x, y)
        self.action = {"kind": "new", "item": item, "start": (x, y), "view_start": (vx, vy)}
        if tool in sd.REGION_TYPES:
            self.action["preview"] = item["r"]
        else:
            self.doc["items"].append(item)
            self.action["index"] = len(self.doc["items"]) - 1

    def _drag_update(self, gesture, dx, dy):
        a = self.action
        if not a:
            return
        ok, sx, sy = gesture.get_start_point()
        x, y = self.to_image(sx + dx, sy + dy)
        if a["kind"] == "crop":
            x0, y0 = a["start"]
            x0, x = sorted((max(0, min(self.img_w, x0)), max(0, min(self.img_w, x))))
            y0, y = sorted((max(0, min(self.img_h, y0)), max(0, min(self.img_h, y))))
            if x - x0 >= 2 and y - y0 >= 2:
                self.doc["crop"] = [round(x0), round(y0), round(x - x0), round(y - y0)]
        elif a["kind"] == "handle":
            sd.drag_handle(self.doc["items"][self.selected], a["name"], x, y)
        elif a["kind"] == "move":
            lx, ly = a["last"]
            sd.move_item(self.doc["items"][self.selected], x - lx, y - ly)
            a["last"] = (x, y)
        elif a["kind"] == "new":
            item = a["item"]
            x0, y0 = a["start"]
            t = item["type"]
            if t in sd.LINE_TYPES:
                item["p"] = [x0, y0, x, y]
            elif t in sd.PATH_TYPES:
                px, py = item["pts"][-2:]
                if math.hypot(x - px, y - py) * self._view()[0] >= 2:
                    item["pts"] += [x, y]
            else:
                item["r"] = [x0, y0, x - x0, y - y0]
                if "preview" in a:
                    a["preview"] = item["r"]
        self.canvas.queue_draw()

    def _drag_end(self, gesture, dx, dy):
        a, self.action = self.action, None
        if not a:
            return
        if a["kind"] in ("crop", "handle", "move"):
            if self.doc != a["before"]:
                self.undo_stack.append(a["before"])
                self.redo_stack.clear()
                self._last_merge = None
            self._changed()
            return
        item = a["item"]
        long_enough = math.hypot(dx, dy) >= MIN_DRAG or item["type"] in sd.PATH_TYPES
        if "index" in a:
            self.doc["items"].pop(a["index"])
        if not long_enough:
            self.canvas.queue_draw()
            return
        if item["type"] in sd.BOX_TYPES:
            item["r"] = sd.norm_box(item["r"])
        self._change(lambda d: d["items"].append(item))
        self.selected = len(self.doc["items"]) - 1
        self._update_options()
        self.canvas.queue_draw()

    def _hover(self, _c, vx, vy):
        if self.tool != "select" or self.action:
            return
        x, y = self.to_image(vx, vy)
        over = self._handle_at(vx, vy) or self._item_at(x, y) is not None
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("move" if over else "default"))

    # ---- text ---------------------------------------------------------------------------

    def _edit_text(self, index, new=False):
        self.editing_text = index
        self._text_new = new
        if not new:
            self._text_before = copy.deepcopy(self.doc)
        item = self.doc["items"][index]
        s = self._view()[0]
        vx, vy = self.to_view(*item["p"])
        self.text_entry.set_margin_start(max(0, round(vx)))
        self.text_entry.set_margin_top(max(0, round(vy)))
        self._quiet_text = True
        self.text_entry.set_text(item["text"])
        self._quiet_text = False
        provider = getattr(self, "_entry_css", None)
        if provider is None:
            provider = self._entry_css = Gtk.CssProvider()
            self.text_entry.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        r, g, b = sd.COLORS.get(item["color"], sd.COLORS["red"])
        provider.load_from_string(f"entry {{ font-size: {max(10, round(item['size'] * s * 0.75))}pt; "
                                  f"color: rgb({round(r * 255)},{round(g * 255)},{round(b * 255)}); }}")
        self.text_entry.set_visible(True)
        self.text_entry.grab_focus()
        self.selected = index
        self.canvas.queue_draw()

    def _text_changed(self):
        if getattr(self, "_quiet_text", False) or self.editing_text is None:
            return
        self.doc["items"][self.editing_text]["text"] = self.text_entry.get_text()

    def _finish_text(self):
        if self.editing_text is None:
            return
        index, self.editing_text = self.editing_text, None
        self.text_entry.set_visible(False)
        item = self.doc["items"][index]
        item["text"] = self.text_entry.get_text().strip()
        if not item["text"]:
            self.doc["items"].pop(index)
            self.selected = None
            if self._text_new and self.undo_stack:
                self.undo_stack.pop()   # an empty new text leaves no trace
        elif not self._text_new and self.doc != self._text_before:
            self.undo_stack.append(self._text_before)
            self.redo_stack.clear()
        self._last_merge = None
        self.canvas.grab_focus()
        self._changed()

    # ---- keys ---------------------------------------------------------------------------

    def _key(self, _c, keyval, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        if self.editing_text is not None:
            if keyval == Gdk.KEY_Escape:
                self._finish_text()
                return True
            return False
        name = Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or ""
        if ctrl:
            if name == "z":
                self.redo() if shift else self.undo()
            elif name == "y":
                self.redo()
            elif name == "c":
                self.copy_text() if shift else self.copy()
            elif name == "s":
                self.save_as() if shift else self.save()
            else:
                return False
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self._delete_selected()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.tool == "crop":
                self.tool_buttons["select"].set_active(True)
            elif self.selected is not None:
                self.selected = None
                self._update_options()
                self.canvas.queue_draw()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.tool == "crop":
            self.tool_buttons["select"].set_active(True)
            return True
        if name in TOOL_KEYS and not state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            self.tool_buttons[TOOL_KEYS[name]].set_active(True)
            return True
        return False

    # ---- output -------------------------------------------------------------------------

    def _render_to(self, path):
        self._finish_text()
        return sd.export_png(self.base, self.doc, path)

    def save(self):
        try:
            self._render_to(self.path)
        except (OSError, cairo.Error) as e:
            self._toast(f"Couldn't save: {e}")
            return
        self.saved_doc = copy.deepcopy(self.doc)
        self._changed()
        self._toast(f"Saved to {self.path.parent.name}/{self.path.name}")

    def save_as(self):
        from .shots import shot_destination
        dialog = Gtk.FileDialog(title="Save screenshot as", initial_name=shot_destination(self.path.parent).name)
        dialog.set_initial_folder(Gio.File.new_for_path(str(self.path.parent)))

        def picked(d, result):
            try:
                f = d.save_finish(result)
            except GLib.Error:
                return
            path = Path(f.get_path())
            if path.suffix.lower() != ".png":
                path = path.with_suffix(".png")
            try:
                self._render_to(path)
            except (OSError, cairo.Error) as e:
                self._toast(f"Couldn't save: {e}")
                return
            self.saved_doc = copy.deepcopy(self.doc)
            self._changed()
            self._toast(f"Saved to {path.parent.name}/{path.name}")

        dialog.save(self, None, picked)

    def _temp_render(self, name):
        from .shots import WORK_DIR
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        path = WORK_DIR / name
        self._render_to(path)
        return path

    def copy(self):
        from .shots import copy_image
        if copy_image(self._temp_render("copy.png")):
            self._toast("Copied to the clipboard")
        else:
            self._toast("Copying requires wl-clipboard")

    def copy_text(self):
        """Reads the text in the cropped picture. Blurred and blocked areas stay hidden."""
        from . import ocr
        self._finish_text()
        regions = [it for it in self.doc["items"] if it["type"] in sd.REGION_TYPES]
        flat = sd.flatten(self.base, regions)
        cx, cy, cw, ch = sd.crop_rect(self.doc, self.img_w, self.img_h)
        area = cairo.ImageSurface(cairo.FORMAT_ARGB32, cw, ch)
        cr = cairo.Context(area)
        cr.set_source_surface(flat, -cx, -cy)
        cr.paint()
        self.text_btn.set_sensitive(False)
        self.text_btn.set_label("Reading…")

        def done(text, error):
            self.text_btn.set_sensitive(True)
            self.text_btn.set_label("Copy Text")
            if error:
                self._toast(error)
            elif not text:
                self._toast("No text found")
            elif ocr.copy_text(text):
                lines = text.count("\n") + 1
                self._toast(f"Copied {lines} line{'s' if lines != 1 else ''} of text")
            else:
                self._toast("Copying requires wl-clipboard")

        ocr.read_text_async(area, done)

    def pin(self):
        if self.on_pin:
            stamp = GLib.DateTime.new_now_local().format("%H%M%S")
            self.on_pin(str(self._temp_render(f"pin-{stamp}.png")))

    def _show_folder(self):
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(str(self.path.parent)), None)

    def delete(self):
        dialog = Adw.AlertDialog(heading="Delete this screenshot?",
                                 body=f"“{self.path.name}” goes to the trash.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def answered(_d, response):
            if response != "delete":
                return
            try:
                Gio.File.new_for_path(str(self.path)).trash(None)
            except GLib.Error:
                self.path.unlink(missing_ok=True)
            self.saved_doc = copy.deepcopy(self.doc)
            self.close()

        dialog.connect("response", answered)
        dialog.present(self)

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # ---- closing ------------------------------------------------------------------------

    def _on_close_request(self, *_):
        self._finish_text()
        if self.doc != self.saved_doc and self.path.exists():
            dialog = Adw.AlertDialog(heading="Save Changes?",
                                     body="Your annotations will be lost if you don't save them.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("discard", "Discard")
            dialog.add_response("save", "Save")
            dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

            def answered(_d, response):
                if response == "save":
                    self.save()
                if response in ("save", "discard"):
                    self.saved_doc = copy.deepcopy(self.doc)
                    self.close()

            dialog.connect("response", answered)
            dialog.present(self)
            return True
        if self.on_closed:
            cb, self.on_closed = self.on_closed, None
            cb()
        return False
