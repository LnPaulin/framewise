"""Editor: background, shape and overlays (captions, pictures, blurs).

A mixin for the Editor window. Everything goes through Editor._commit, so
undo, saving the edit and refreshing the preview work as for any change.
"""

import shutil
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import compose, motion  # noqa: E402
from . import edit_model as em  # noqa: E402
from . import export  # noqa: E402
from . import looks  # noqa: E402
from .filmstrip import BlockLane  # noqa: E402

KINDS = {"text": ("texts", "Text"), "image": ("images", "Image"), "blur": ("blurs", "Blur")}
ANCHOR_SYMBOLS = ["↖", "↑", "↗", "←", "•", "→", "↙", "↓", "↘"]
SHAPES = [("original", "Original"), ("wide", "Wide 16:9"), ("square", "Square"), ("vertical", "Tall 9:16")]
COLOR_NAMES = list(compose.TEXT_COLORS)

SWATCH_CSS = "".join(
    f".ck-swatch-{name} {{ background-image: linear-gradient(135deg, "
    f"rgb({a[0]*255:.0f},{a[1]*255:.0f},{a[2]*255:.0f}), "
    f"rgb({b[0]*255:.0f},{b[1]*255:.0f},{b[2]*255:.0f})); min-width: 34px; min-height: 26px; "
    f"border-radius: 7px; }}\n"
    for name, (a, b) in compose.PRESETS.items()
) + ".ck-swatch-none { min-width: 34px; min-height: 26px; border-radius: 7px; }\n" \
    ".ck-swatch-selected { outline: 2px solid @accent_color; outline-offset: 2px; }\n"


class StyleMixin:
    # ---- building ----------------------------------------------------------------------

    def _init_style(self):
        self._textures = {}
        self._overlay_before = None
        self._scene_timer = None
        provider = Gtk.CssProvider()
        provider.load_from_string(SWATCH_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        area = self.crop_px if self.crop_px else (0, 0, *self.video_size)
        self.src_size = (area[2], area[3])

    def _build_style_button(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                       margin_bottom=12, margin_start=12, margin_end=12, width_request=320)
        body.append(Gtk.Label(label="Presets", xalign=0, css_classes=["heading"]))
        looks_row = Gtk.Box(css_classes=["linked"], homogeneous=True)
        for key, label, tip in (("original", "Original", "The recording as it is"),
                                ("smooth", "Smooth", "Smooth pointer, click rings and zoom on clicks"),
                                ("studio", "Studio", "Smooth, on a soft background with rounded corners")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", lambda _b, k=key: self._apply_look(k))
            looks_row.append(b)
        body.append(looks_row)
        # Looks (presets) saved by the user.
        self._my_looks = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3,
                                     min_children_per_line=1, column_spacing=6, row_spacing=6)
        body.append(self._my_looks)
        save_look = Gtk.Button(css_classes=["flat"], halign=Gtk.Align.START,
                               tooltip_text="Save the background, shape, cursor, zoom and "
                                            "images as a preset")
        save_look.set_child(self._icon_label("list-add-symbolic", "Save Preset…"))
        save_look.connect("clicked", lambda *_: self._ask_look_name())
        body.append(save_look)
        self._fill_my_looks()

        body.append(Gtk.Label(label="Background", xalign=0, css_classes=["heading"], margin_top=6))
        swatches = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                               min_children_per_line=4, column_spacing=6, row_spacing=6)
        self._swatches = {}
        none = Gtk.Button(css_classes=["ck-swatch-none"], tooltip_text="No background",
                          child=Gtk.Image(icon_name="action-unavailable-symbolic"))
        none.connect("clicked", lambda *_: self._set_background(kind="none"))
        swatches.append(none)
        self._swatches["none"] = none
        for name in compose.PRESETS:
            b = Gtk.Button(css_classes=[f"ck-swatch-{name}"], tooltip_text=name.capitalize())
            b.connect("clicked", lambda _b, n=name: self._set_background(kind="preset", preset=n))
            swatches.append(b)
            self._swatches[name] = b
        self.bg_color = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                              tooltip_text="Custom color")
        self.bg_color.connect("notify::rgba", self._on_bg_color)
        swatches.append(self.bg_color)
        pick = Gtk.Button(icon_name="image-x-generic-symbolic", tooltip_text="Custom image")
        pick.connect("clicked", lambda *_: self._pick_bg_image())
        swatches.append(pick)
        body.append(swatches)

        self.margin_scale = self._style_slider(body, "Margin", "padding")
        self.radius_scale = self._style_slider(body, "Corner roundness", "radius")
        self.bgblur_scale = self._style_slider(body, "Picture blur", "blur")

        body.append(Gtk.Label(label="Shape", xalign=0, css_classes=["heading"], margin_top=6))
        shapes = Gtk.Box(css_classes=["linked"], homogeneous=True)
        self._shape_buttons = {}
        first = None
        for key, label in SHAPES:
            b = Gtk.ToggleButton(label=label)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_aspect(k))
            shapes.append(b)
            self._shape_buttons[key] = b
        body.append(shapes)
        body.append(Gtk.Label(
            label="With a background, the video grows around the recording. "
                  "Without one, a shape crops the middle of the recording.",
            xalign=0, wrap=True, max_width_chars=42, css_classes=["dim-label", "caption"]))
        button = Gtk.MenuButton(popover=Gtk.Popover(child=body), tooltip_text="Background and shape")
        button.set_child(self._icon_label("applications-graphics-symbolic", "Style"))
        return button

    def _style_slider(self, box, title, key):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row.append(Gtk.Label(label=title, xalign=0))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        scale.set_draw_value(False)
        scale.connect("value-changed", lambda s, k=key: self._on_style_slider(k, s.get_value()))
        row.append(scale)
        box.append(row)
        scale.row = row
        return scale

    def _build_add_button(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8,
                       margin_bottom=8, margin_start=8, margin_end=8)
        for kind, icon, label, tip in (
                ("text", "insert-text-symbolic", "Text", "A caption over the video"),
                ("image", "insert-image-symbolic", "Image…", "A logo or picture over the video"),
                ("blur", "view-conceal-symbolic", "Blur an area", "Hide a name, an address or a number")):
            b = Gtk.Button(css_classes=["flat"], tooltip_text=tip)
            b.set_child(self._icon_label(icon, label))
            b.connect("clicked", lambda _b, k=kind: (self._add_popover.popdown(), self._add_overlay(k)))
            body.append(b)
        self._add_popover = Gtk.Popover(child=body)
        button = Gtk.MenuButton(popover=self._add_popover, tooltip_text="Add text, an image or a blur region")
        button.set_child(self._icon_label("list-add-symbolic", "Add"))
        return button

    def _build_overlay_lanes(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=12, margin_end=12)
        self.overlay_lanes = {}
        for kind, (_key, label) in KINDS.items():
            lane = BlockLane(self.strip, kind, label)
            lane.on_change = lambda items, done, k=kind: self._on_lane_change(k, items, done)
            lane.on_select = lambda i, k=kind: self._on_overlay_selected(k, i)
            lane.on_add = lambda t, k=kind: self._add_overlay(k, t)
            lane.set_visible(False)
            box.append(lane)
            self.overlay_lanes[kind] = lane
        return box

    def _build_overlay_bar(self):
        self.overlay_stack = Gtk.Stack()
        # Text
        self.text_entry = Gtk.Entry(hexpand=True, placeholder_text="Text")
        self.text_entry.connect("changed", lambda e: self._set_item(text=e.get_text(), merge="text"))
        self.text_size = self._item_slider(0.03, 0.16, 0.005, "size", "Size")
        self.text_color = Gtk.DropDown.new_from_strings([c.capitalize() for c in COLOR_NAMES])
        self.text_color.connect("notify::selected", lambda d, _p: self._set_item(
            color=COLOR_NAMES[d.get_selected()]))
        text_box = Gtk.Box(spacing=8)
        for w in (self.text_entry, self.text_size.box, self.text_color, self._anchor_button("text")):
            text_box.append(w)
        self.overlay_stack.add_named(text_box, "text")
        # Image
        self.image_size = self._item_slider(0.04, 0.6, 0.01, "size", "Size")
        self.image_opacity = self._item_slider(0.05, 1.0, 0.05, "opacity", "Opacity")
        image_box = Gtk.Box(spacing=8)
        for w in (self.image_size.box, self.image_opacity.box, self._anchor_button("image")):
            image_box.append(w)
        self.overlay_stack.add_named(image_box, "image")
        # Blur
        draw = Gtk.Button(label="Select Area", tooltip_text="Drag over the area to hide")
        draw.connect("clicked", lambda *_: self._start_drawing())
        self.blur_strength = self._item_slider(1, 5, 1, "strength", "Strength")
        self.blur_hint = Gtk.Label(css_classes=["dim-label"])
        blur_box = Gtk.Box(spacing=8)
        for w in (draw, self.blur_strength.box, self.blur_hint):
            blur_box.append(w)
        self.overlay_stack.add_named(blur_box, "blur")

        remove = Gtk.Button(label="Remove", css_classes=["flat"])
        remove.connect("clicked", lambda *_: self._remove_overlay())
        self.overlay_title = Gtk.Label(css_classes=["heading"])
        box = Gtk.Box(spacing=10, margin_start=12, margin_end=12, margin_top=4)
        box.append(self.overlay_title)
        self.overlay_stack.set_hexpand(True)
        box.append(self.overlay_stack)
        box.append(remove)
        self.overlay_bar = Gtk.Revealer(child=box, reveal_child=False,
                                        transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._selected_overlay = None   # (kind, index)
        return self.overlay_bar

    def _item_slider(self, lo, hi, step, key, title):
        box = Gtk.Box(spacing=4)
        box.append(Gtk.Label(label=title, css_classes=["dim-label"]))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_draw_value(False)
        scale.set_size_request(120, -1)
        scale.connect("value-changed", lambda s: self._set_item(
            **{key: int(s.get_value()) if key == "strength" else round(s.get_value(), 3)},
            merge=key))
        box.append(scale)
        scale.box = box
        return scale

    def _anchor_button(self, kind):
        grid = Gtk.Grid(column_spacing=4, row_spacing=4, margin_top=6, margin_bottom=6,
                        margin_start=6, margin_end=6)
        for i, anchor in enumerate(compose.ANCHORS):
            b = Gtk.Button(label=ANCHOR_SYMBOLS[i], width_request=36, height_request=30)
            b.connect("clicked", lambda _b, a=anchor: self._set_item(anchor=a))
            grid.attach(b, i % 3, i // 3, 1, 1)
        button = Gtk.MenuButton(label="Position", popover=Gtk.Popover(child=grid),
                                tooltip_text="Placement on the video")
        return button

    # ---- style -----------------------------------------------------------------------

    def _style_doc(self, **bg_changes):
        doc = em.clone(self.doc)
        doc["style"]["background"].update(bg_changes)
        return doc

    def _apply_look(self, look):
        """The three built-in starting points. A look only sets values."""
        doc = em.clone(self.doc)
        p = doc["pointer"]
        if look == "original":
            doc["style"] = compose.default_style()
            p.update(show=True, smoothing="off", rings=False)
            doc["zoom"]["enabled"] = False
        else:
            p.update(show=True, smoothing="smooth", rings=True)
            doc["zoom"]["enabled"] = True
            if look == "smooth":
                doc["style"] = compose.default_style()
            else:
                doc["style"]["background"].update(kind="preset", preset="graphite",
                                                  padding=0.45, radius=0.35)
        self._commit(doc)

    def _fill_my_looks(self):
        while (child := self._my_looks.get_first_child()) is not None:
            self._my_looks.remove(child)
        for look in looks.all_looks():
            pair = Gtk.Box(css_classes=["linked"])
            use = Gtk.Button(label=look["name"], tooltip_text=f"Use “{look['name']}”")
            use.connect("clicked", lambda _b, lk=look: self._use_look(lk))
            remove = Gtk.Button(icon_name="window-close-symbolic",
                                tooltip_text=f"Remove “{look['name']}”")
            remove.connect("clicked", lambda _b, lk=look: self._remove_look(lk))
            pair.append(use)
            pair.append(remove)
            self._my_looks.append(pair)
        self._my_looks.set_visible(bool(looks.all_looks()))

    def _ask_look_name(self):
        dialog = Adw.AlertDialog(heading="Save Preset",
                                 body="Background, shape, cursor, zoom and images. "
                                      "Cuts, captions and audio stay with this recording.")
        entry = Gtk.Entry(placeholder_text="Preset name, for example “Tutorials”", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_response_enabled("save", False)
        entry.connect("changed", lambda e: dialog.set_response_enabled("save", bool(e.get_text().strip())))

        def answered(_d, response):
            name = entry.get_text().strip()
            if response == "save" and name:
                looks.save_look(name, self.doc, self.take_dir)
                self._fill_my_looks()
                self._toast(f"Preset “{name}” saved")

        dialog.connect("response", answered)
        dialog.present(self)

    def _use_look(self, look):
        def store(path, absolute=False):
            stored = self._store_picture(path)
            return str(self.take_dir / stored) if absolute else stored
        self._commit(looks.apply_look(look, self.doc, self.duration, store))

    def _remove_look(self, look):
        looks.remove_look(look["id"])
        self._fill_my_looks()
        self._toast(f"Preset “{look['name']}” removed")

    # ---- pointer ----------------------------------------------------------------------

    def _build_pointer_button(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                       margin_bottom=12, margin_start=12, margin_end=12, width_request=300)
        body.append(Gtk.Label(label="Cursor", xalign=0, css_classes=["heading"]))
        rows = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.ptr_show = Adw.SwitchRow(title="Custom cursor")
        self.ptr_rings = Adw.SwitchRow(title="Click highlights",
                                       subtitle="Shows a ring at each click")
        rows.append(self.ptr_show)
        rows.append(self.ptr_rings)
        body.append(rows)
        self.ptr_show.connect("notify::active", lambda r, _p: self._set_pointer(show=r.get_active()))
        self.ptr_rings.connect("notify::active", lambda r, _p: self._set_pointer(rings=r.get_active()))

        body.append(Gtk.Label(label="Smoothing", xalign=0))
        smooth = Gtk.Box(css_classes=["linked"], homogeneous=True)
        self.ptr_smoothing = {}
        first = None
        for key, label in (("off", "None"), ("light", "Light"), ("smooth", "Smooth"),
                           ("cinematic", "Cinematic")):
            b = Gtk.ToggleButton(label=label)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_pointer(smoothing=k))
            smooth.append(b)
            self.ptr_smoothing[key] = b
        body.append(smooth)

        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label="Size", xalign=0, hexpand=True))
        self.ptr_size_label = Gtk.Label(css_classes=["dim-label"])
        row.append(self.ptr_size_label)
        body.append(row)
        self.ptr_size = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.5, 2.0, 0.05)
        self.ptr_size.set_draw_value(False)
        self.ptr_size.connect("value-changed", lambda sc: self._set_pointer(
            size=round(sc.get_value(), 2), merge="pointer-size"))
        body.append(self.ptr_size)
        drawn = self.doc["pointer"]["drawn"]
        note = ("The pointer glides instead of jittering, lands exactly on every click, and "
                "fades out when it rests." if drawn else
                "This recording has the pointer in the picture, so it can't be restyled. "
                "New screen and area recordings can (needs the add-on, version 0.6).")
        body.append(Gtk.Label(label=note, xalign=0, wrap=True, max_width_chars=40,
                              css_classes=["dim-label", "caption"]))
        for w in (rows, smooth, self.ptr_size):
            w.set_sensitive(drawn)
        button = Gtk.MenuButton(popover=Gtk.Popover(child=body), tooltip_text="Cursor")
        button.set_child(self._icon_label("input-mouse-symbolic", "Cursor"))
        return button

    def _set_pointer(self, merge=None, **changes):
        if getattr(self, "_refreshing", False):
            return
        doc = em.clone(self.doc)
        doc["pointer"].update(changes)
        same = bool(self.undo_stack) and merge and getattr(self, "_last_gain_track", None) == merge
        self._commit(doc, remember=not same, gain_track=merge)

    def _refresh_pointer(self):
        p = self.doc["pointer"]
        self.ptr_show.set_active(p["show"])
        self.ptr_rings.set_active(p["rings"])
        self.ptr_smoothing[p["smoothing"]].set_active(True)
        self.ptr_size.set_value(p["size"])
        self.ptr_size_label.set_label(f"{p['size']:.2f}×")

    def _set_background(self, **changes):
        self._commit(self._style_doc(**changes))

    def _on_bg_color(self, button, _p):
        if getattr(self, "_refreshing", False):
            return
        c = button.get_rgba()
        self._set_background(kind="solid", color=[round(c.red, 3), round(c.green, 3), round(c.blue, 3)])

    def _pick_bg_image(self):
        self._pick_picture("Choose a background picture", self._use_bg_image)

    def _use_bg_image(self, path):
        stored = self._store_picture(path)
        self._set_background(kind="image", image=str(self.take_dir / stored), blur=0.3)

    def _on_style_slider(self, key, value):
        if getattr(self, "_refreshing", False):
            return
        merge = bool(self.undo_stack) and getattr(self, "_last_gain_track", None) == f"style-{key}"
        self._commit(self._style_doc(**{key: round(value, 3)}), remember=not merge,
                     gain_track=f"style-{key}")

    def _set_aspect(self, aspect):
        if getattr(self, "_refreshing", False) or self.doc["style"]["aspect"] == aspect:
            return
        doc = em.clone(self.doc)
        doc["style"]["aspect"] = aspect
        self._commit(doc)

    def _refresh_style(self):
        style = self.doc["style"]
        bg = style["background"]
        for name, b in self._swatches.items():
            chosen = (bg["kind"] == "none" and name == "none") or (
                bg["kind"] == "preset" and bg.get("preset") == name)
            (b.add_css_class if chosen else b.remove_css_class)("ck-swatch-selected")
        self.margin_scale.set_value(bg["padding"])
        self.radius_scale.set_value(bg["radius"])
        self.bgblur_scale.set_value(bg.get("blur", 0))
        has_bg = bg["kind"] != "none"
        self.margin_scale.row.set_sensitive(has_bg)
        self.radius_scale.row.set_sensitive(has_bg)
        self.bgblur_scale.row.set_visible(bg["kind"] == "image")
        self._shape_buttons[style["aspect"]].set_active(True)
        self._refresh_pointer()
        for kind, (key, _label) in KINDS.items():
            self.overlay_lanes[kind].set_items(self.doc[key])
        self._refresh_overlay_bar()

    # ---- pictures (image overlays and backgrounds) ------------------------------------

    def _pick_picture(self, title, then):
        dialog = Gtk.FileDialog(title=title)
        flt = Gtk.FileFilter(name="Pictures")
        for mime in ("image/png", "image/jpeg", "image/webp", "image/svg+xml", "image/gif"):
            flt.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)

        def done(d, result):
            try:
                f = d.open_finish(result)
            except GLib.Error:
                return
            then(f.get_path())

        dialog.open(self, None, done)

    def _store_picture(self, path):
        """A private copy next to the recording, so moving the original can't break the edit."""
        folder = self.take_dir / "pictures"
        folder.mkdir(exist_ok=True)
        src = Path(path)
        dest = folder / src.name
        n = 2
        while dest.exists() and dest.stat().st_size != src.stat().st_size:
            dest = folder / f"{src.stem}-{n}{src.suffix}"
            n += 1
        if not dest.exists():
            shutil.copy2(src, dest)
        return str(dest.relative_to(self.take_dir))

    # ---- overlays ---------------------------------------------------------------------

    def _add_overlay(self, kind, at=None):
        t = self.player.position() if at is None else at
        if kind == "image":
            self._pick_picture("Choose a picture to show", lambda p: self._insert_image(p, t))
            return
        doc = em.clone(self.doc)
        item = {"id": em.new_id(), "start": round(t, 3)}
        if kind == "text":
            item.update(end=round(min(self.duration, t + 3.0), 3), text="Text",
                        size=0.06, color="white", anchor="bottom")
            doc["texts"].append(item)
        else:
            item.update(end=round(self.duration, 3), rect=[0.35, 0.4, 0.3, 0.2], strength=3)
            doc["blurs"].append(item)
        self._commit(doc)
        self._select_by_id(kind, item["id"])
        if kind == "text":
            self.text_entry.grab_focus()
            self.text_entry.select_region(0, -1)
        else:
            self._start_drawing()

    def _insert_image(self, path, t):
        stored = self._store_picture(path)
        doc = em.clone(self.doc)
        item = {"id": em.new_id(), "start": round(t, 3), "end": round(self.duration, 3),
                "file": stored, "size": 0.18, "opacity": 1.0, "anchor": "topTrailing"}
        doc["images"].append(item)
        self._commit(doc)
        self._select_by_id("image", item["id"])

    def _select_by_id(self, kind, item_id):
        key = KINDS[kind][0]
        index = next((i for i, it in enumerate(self.doc[key]) if it["id"] == item_id), None)
        self.overlay_lanes[kind].set_items(self.doc[key])
        self.overlay_lanes[kind].select(index)

    def _on_overlay_selected(self, kind, index):
        for other, lane in self.overlay_lanes.items():
            if other != kind:
                lane.select(None, notify=False)
        if index is not None and self.lane.selected is not None:
            self.lane.select(None)  # one selection at a time (zooms too)
        self._selected_overlay = (kind, index) if index is not None else None
        self.picture.set_drawing(False)
        self._refresh_overlay_bar()

    def _selected_item(self):
        if not self._selected_overlay:
            return None, None, None
        kind, index = self._selected_overlay
        items = self.doc[KINDS[kind][0]]
        if index is None or index >= len(items):
            return None, None, None
        return kind, index, items[index]

    def _refresh_overlay_bar(self):
        kind, _i, item = self._selected_item()
        self.overlay_bar.set_reveal_child(item is not None)
        if item is None:
            return
        was, self._refreshing = getattr(self, "_refreshing", False), True
        self.overlay_stack.set_visible_child_name(kind)
        self.overlay_title.set_label({"text": "This text:", "image": "This image:",
                                      "blur": "This blur:"}[kind])
        if kind == "text":
            if self.text_entry.get_text() != item["text"]:
                self.text_entry.set_text(item["text"])
            self.text_size.set_value(item["size"])
            self.text_color.set_selected(COLOR_NAMES.index(item["color"]))
        elif kind == "image":
            self.image_size.set_value(item["size"])
            self.image_opacity.set_value(item["opacity"])
        else:
            self.blur_strength.set_value(item["strength"])
            self.blur_hint.set_label("Hidden for the duration of its block on the timeline")
        self._refreshing = was

    def _set_item(self, merge=None, **changes):
        if getattr(self, "_refreshing", False):
            return
        kind, index, item = self._selected_item()
        if item is None:
            return
        doc = em.clone(self.doc)
        doc[KINDS[kind][0]][index].update(changes)
        tag = f"{kind}-{item['id']}-{merge}" if merge else None
        same = bool(self.undo_stack) and tag and getattr(self, "_last_gain_track", None) == tag
        self._commit(doc, remember=not same, gain_track=tag)

    def _remove_overlay(self):
        kind, index, item = self._selected_item()
        if item is None:
            return
        doc = em.clone(self.doc)
        del doc[KINDS[kind][0]][index]
        self._selected_overlay = None
        self.overlay_lanes[kind].select(None, notify=False)
        self.picture.set_drawing(False)
        self._commit(doc)

    def _on_lane_change(self, kind, items, done):
        key = KINDS[kind][0]
        if not done:
            if self._overlay_before is None:
                self._overlay_before = self.doc
            doc = em.clone(self.doc)
            doc[key] = items
            self.doc = doc
            self._update_scene()
            return
        before, self._overlay_before = self._overlay_before, None
        after = em.clone(self.doc)
        after[key] = items
        if before is not None:
            self.doc = before
        self._commit(after)

    def _start_drawing(self):
        kind, _i, item = self._selected_item()
        if kind != "blur":
            return
        if self.player.playing:
            self.player.pause()
        self.picture.on_rect = self._on_blur_rect
        self.picture.set_drawing(True, item["rect"])
        self.blur_hint.set_label("Drag over the preview to select the area to hide")

    def _on_blur_rect(self, rect):
        self.picture.set_drawing(False)
        self._set_item(rect=[round(v, 4) for v in rect])
        self.blur_hint.set_label("✓ Area selected")

    # ---- preview ----------------------------------------------------------------------

    def _texture(self, path):
        key = str(path)
        if key in self._textures:
            self._textures[key] = self._textures.pop(key)      # most recently used last
        else:
            self._textures[key] = Gdk.Texture.new_from_filename(key)
            # A slider drag makes a background per step (~20 MB each when large):
            # keep only the recent ones.
            while len(self._textures) > 12:
                self._textures.pop(next(iter(self._textures)))
        return self._textures[key]

    def _pointer_scene(self):
        """The drawn pointer for the preview (same track and image as the export)."""
        from . import pointer as ptr
        p = self.doc["pointer"]
        if not (p["drawn"] and p["show"] and self.area_samples):
            return None
        key = p["smoothing"]
        if getattr(self, "_track_key", None) != key:
            self._track = ptr.track(self.area_samples, self.duration, self.fps, p["smoothing"])
            self._track_key = key
        px = ptr.pixel_size(self.src_size[1], p["size"])
        path = self.take_dir / "render" / f"arrow-{px}.png"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            pad, _size = ptr.arrow_png(path, px)
        pad = max(2, int(px * 0.12))
        frames, rings = self._track
        return {"frames": frames, "rings": rings, "texture": self._texture(path), "pad": pad,
                "px": px, "rings_on": p["rings"]}

    def _update_scene(self):
        """Shows a style change at once, then the exact picture once it settles.

        At once: the new layout (margin, corners, shape) with the background
        and captions already made, stretched to the new size, so a slider
        follows the mouse. Then, shortly after changes stop, the exact
        background and captions are drawn in a background thread (a large
        or blurred background takes a while) and swapped in."""
        lay = compose.layout(*self.src_size, self.doc["style"])
        if self.picture.layout is not None:
            bg = self.picture.background if lay["background"] else None
            self.picture.set_scene(lay, self.src_size, bg, self.picture.overlays, self.doc["blurs"])
        if self._scene_timer:
            GLib.source_remove(self._scene_timer)
        self._scene_timer = GLib.timeout_add(150, self._render_scene)

    def _render_scene(self):
        self._scene_timer = None
        if getattr(self, "_scene_busy", False):
            self._scene_again = True          # one job at a time: redo when it ends
            return False
        self._scene_busy, self._scene_again = True, False
        doc = em.clone(self.doc)
        lay = compose.layout(*self.src_size, doc["style"])
        out_dir, take_dir = self.take_dir / "render", self.take_dir

        def work():
            try:
                assets = export.render_assets(doc, lay, out_dir, take_dir)
            except Exception as e:  # a broken picture must not break the editor
                print("render failed:", e, flush=True)
                assets = {"background": None, "texts": [], "images": []}
            GLib.idle_add(self._scene_ready, doc, lay, assets)

        import threading
        threading.Thread(target=work, daemon=True).start()
        return False

    def _scene_ready(self, doc, lay, assets):
        self._scene_busy = False
        if getattr(self, "_closing", False):
            return False
        if self._scene_again or doc["style"] != self.doc["style"] or doc["texts"] != self.doc["texts"] \
                or doc["images"] != self.doc["images"]:
            # Changed again meanwhile: draw the newest instead of showing this.
            self._scene_timer = GLib.timeout_add(10, self._render_scene)
            return False
        bg = self._texture(assets["background"]) if assets["background"] else None
        overlays = [(item, self._texture(path), place, 1.0, compose.TEXT_FADE)
                    for item, path, place in assets["texts"]]
        overlays += [(item, self._texture(path), place, item["opacity"], compose.IMAGE_FADE)
                     for item, path, place in assets["images"]]
        self.picture.set_scene(lay, self.src_size, bg, overlays, self.doc["blurs"])
        self.picture.set_pointer(self._pointer_scene())
        z = self.doc["zoom"]
        if z["enabled"] and z["segments"]:
            sx, sy, sw, sh = lay["source"]
            samples = [(t, (x - sx) / sw, (y - sy) / sh, *rest)
                       for t, x, y, *rest in self.area_samples]
            typing = [(t, (x - sx) / sw, (y - sy) / sh) for t, x, y in self.typing]
            views = motion.viewports(export.segments_in_base(z["segments"], lay["source"]),
                                     z["amount"], samples, self.duration, self.fps, typing=typing)
            self.picture.set_views(views, self.fps)
        else:
            self.picture.set_views(None, self.fps)
        return False
