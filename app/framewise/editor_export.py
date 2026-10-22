"""Editor: how the finished file is made (speed, quality, size limit, GIF).

The choices live in the document's "export" part, so they are undoable and
kept with the recording. Quality, size limit and the GIF settings are also
remembered for the next recording; speed always starts at 1x.
"""

import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import edit_model as em  # noqa: E402
from . import export  # noqa: E402
from .state import DATA_DIR, settings  # noqa: E402

QUALITY_LABELS = (("high", "High", "Every detail, the biggest file"),
                  ("balanced", "Balanced", "Full size, a smaller file"),
                  ("small", "Small", "Half the size, for quick sharing"))
FIT_LABELS = ("No limit", "10 MB", "25 MB", "50 MB", "100 MB")
GIF_SIZE_LABELS = (("small", "Small"), ("medium", "Medium"), ("large", "Large"))
REMEMBERED = ("quality", "fit_mb", "gif_size", "gif_fps")


def speed_label(speed):
    return f"{speed:g}×"


def format_length(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class ExportMixin:
    def _init_export(self, is_new):
        if is_new:
            # A new recording starts with the export choices as last left.
            remembered = settings.get("export") or {}
            self.doc["export"].update({k: remembered[k] for k in REMEMBERED if k in remembered})
            self.doc = em.normalize(self.doc, self.duration)

    # ---- building ----------------------------------------------------------------------

    def _build_export_button(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12,
                       margin_bottom=12, margin_start=12, margin_end=12, width_request=320)

        body.append(Gtk.Label(label="Speed", xalign=0, css_classes=["heading"]))
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        self._speed_buttons = {}
        for i, v in enumerate(em.SPEED_PRESETS):
            b = Gtk.ToggleButton(label=speed_label(v))
            b.connect("clicked", lambda _b, v=v: self._set_export(speed=v))
            grid.attach(b, i % 4, i // 4, 1, 1)
            self._speed_buttons[v] = b
        body.append(grid)
        self.speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,
                                                    *em.SPEED_RANGE, 0.05)
        self.speed_scale.set_draw_value(False)
        self.speed_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        self.speed_scale.connect("value-changed", lambda sc: self._set_export(
            merge="speed", speed=round(sc.get_value() / 0.05) * 0.05))
        body.append(self.speed_scale)
        self.export_length = Gtk.Label(xalign=0, css_classes=["dim-label"], wrap=True)
        body.append(self.export_length)

        body.append(Gtk.Label(label="Quality", xalign=0, css_classes=["heading"], margin_top=6))
        quality = Gtk.Box(css_classes=["linked"], homogeneous=True)
        self._quality_buttons = {}
        first = None
        for key, label, tip in QUALITY_LABELS:
            b = Gtk.ToggleButton(label=label, tooltip_text=tip, group=first)
            first = first or b
            b.connect("toggled", lambda b, k=key: b.get_active() and self._set_export(quality=k))
            quality.append(b)
            self._quality_buttons[key] = b
        body.append(quality)

        fit_row = Gtk.Box(spacing=8)
        fit_row.append(Gtk.Label(label="Maximum file size", hexpand=True, xalign=0))
        self.fit_dropdown = Gtk.DropDown.new_from_strings(list(FIT_LABELS))
        self.fit_dropdown.set_tooltip_text(
            "Quality and resolution are reduced as needed to fit")
        self.fit_dropdown.connect("notify::selected", lambda d, _p: self._set_export(
            fit_mb=em.FIT_SIZES_MB[d.get_selected()]))
        fit_row.append(self.fit_dropdown)
        body.append(fit_row)
        self.export_output = Gtk.Label(xalign=0, css_classes=["dim-label"], wrap=True)
        body.append(self.export_output)

        body.append(Gtk.Label(label="GIF", xalign=0, css_classes=["heading"], margin_top=6))
        gif_row = Gtk.Box(spacing=8)
        gif_sizes = Gtk.Box(css_classes=["linked"], homogeneous=True, hexpand=True)
        self._gif_size_buttons = {}
        first = None
        for key, label in GIF_SIZE_LABELS:
            b = Gtk.ToggleButton(label=label, group=first,
                                 tooltip_text=f"{em.GIF_SIZES[key]} pixels on the longest side")
            first = first or b
            b.connect("toggled", lambda b, k=key: b.get_active() and self._set_export(gif_size=k))
            gif_sizes.append(b)
            self._gif_size_buttons[key] = b
        gif_row.append(gif_sizes)
        self.gif_fps = Gtk.DropDown.new_from_strings([f"{f} fps" for f in em.GIF_FPS])
        self.gif_fps.set_tooltip_text("Frame rate: higher is smoother but makes a larger file")
        self.gif_fps.connect("notify::selected", lambda d, _p: self._set_export(
            gif_fps=em.GIF_FPS[d.get_selected()]))
        gif_row.append(self.gif_fps)
        body.append(gif_row)
        gif_buttons = Gtk.Box(spacing=6, homogeneous=True)
        save_gif = Gtk.Button(label="Save as GIF…")
        save_gif.connect("clicked", lambda *_: (self._export_popover.popdown(), self.save_gif_as()))
        copy_gif = Gtk.Button(label="Copy as GIF")
        copy_gif.connect("clicked", lambda *_: (self._export_popover.popdown(), self.copy_gif()))
        gif_buttons.append(save_gif)
        gif_buttons.append(copy_gif)
        body.append(gif_buttons)

        body.append(Gtk.Label(
            label="The preview plays at 1×. The original recording is never changed.",
            xalign=0, wrap=True, css_classes=["dim-label", "caption"], margin_top=4))

        self._export_popover = Gtk.Popover(child=body)
        self.export_label = Gtk.Label()
        button = Gtk.MenuButton(popover=self._export_popover, tooltip_text="Export settings")
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image(icon_name="emblem-system-symbolic"))
        box.append(self.export_label)
        button.set_child(box)
        return button

    # ---- changes ------------------------------------------------------------------------

    def _set_export(self, merge=None, **changes):
        if getattr(self, "_refreshing", False):
            return
        doc = em.clone(self.doc)
        doc["export"].update(changes)
        doc = em.normalize(doc, self.duration)
        remembered = settings.get("export") or {}
        remembered.update({k: doc["export"][k] for k in REMEMBERED})
        settings.set("export", remembered)
        # Slider moves merge into one undo step, like the volume sliders.
        same = merge and bool(self.undo_stack) and getattr(self, "_last_gain_track", None) == merge
        self._commit(doc, remember=not same, gain_track=merge)
        # Pressing the speed already chosen changes nothing: show it chosen again.
        self._refreshing = True
        self._refresh_export()
        self._refreshing = False

    def _refresh_export(self):
        ex = self.doc["export"]
        for v, b in self._speed_buttons.items():
            b.set_active(abs(v - ex["speed"]) < 1e-3)
        self.speed_scale.set_value(ex["speed"])
        self._quality_buttons[ex["quality"]].set_active(True)
        for b in self._quality_buttons.values():
            b.set_sensitive(not ex["fit_mb"])
        self.fit_dropdown.set_selected(em.FIT_SIZES_MB.index(ex["fit_mb"])
                                       if ex["fit_mb"] in em.FIT_SIZES_MB else 0)
        self._gif_size_buttons[ex["gif_size"]].set_active(True)
        self.gif_fps.set_selected(em.GIF_FPS.index(ex["gif_fps"]))

        length = em.output_duration(self.doc)
        self.export_length.set_label(
            f"The saved video lasts {format_length(length)}"
            + ("" if ex["speed"] == 1 else
               f" (instead of {format_length(em.finished_duration(self.doc))}). "
               "Voices keep their normal pitch."))
        has_audio = any(not self.doc["muted"].get(n) for n in self.tracks)
        out = export.output_settings(self.doc, self.video_size, self.info.get("crop"),
                                     self.fps, length, has_audio)
        if out is None:
            self.export_output.set_label(f"Too long to fit in {ex['fit_mb']} MB. "
                                         "Choose a bigger size, a faster speed, or cut it shorter.")
        else:
            w, h = out.get("size") or export.picture_size(self.video_size, self.info.get("crop"),
                                                          self.doc)
            note = f"{w} × {h}, {out['fps']:g} frames a second"
            if ex["fit_mb"]:
                note += f", under {ex['fit_mb']} MB"
            self.export_output.set_label(note)
        parts = [speed_label(ex["speed"])]
        parts.append(f"≤ {ex['fit_mb']} MB" if ex["fit_mb"] else
                     dict((k, l) for k, l, _t in QUALITY_LABELS)[ex["quality"]])
        self.export_label.set_label(" · ".join(parts))

    # ---- GIF ----------------------------------------------------------------------------

    def save_gif_as(self):
        folder = Path(settings.get("folder")).expanduser()
        dialog = Gtk.FileDialog(title="Save as GIF",
                                initial_name=export.destination(folder).with_suffix(".gif").name)
        dialog.set_initial_folder(Gio_file(folder))

        def picked(d, result):
            try:
                f = d.save_finish(result)
            except GLib.Error:
                return
            path = Path(f.get_path())
            if path.suffix.lower() != ".gif":
                path = path.with_suffix(".gif")
            self._export(path, self._on_gif_saved, gif=True)

        dialog.save(self, None, picked)

    def _on_gif_saved(self, ok, message):
        if not ok:
            if message != "cancelled":
                self._toast(f"The GIF could not be saved: {message[:120]}")
            return
        toast = Adw.Toast(title=f"GIF saved: {Path(message).name}", button_label="Show")
        toast.connect("button-clicked", lambda *_: open_folder(Path(message).parent))
        self.toasts.add_toast(toast)

    def copy_gif(self):
        clips = DATA_DIR / "clips"
        clips.mkdir(parents=True, exist_ok=True)
        dest = export.destination(clips).with_suffix(".gif")

        def done(ok, message):
            if not ok:
                if message != "cancelled":
                    self._toast("The GIF could not be made")
                return
            if copy_file_to_clipboard(message):
                self._toast("GIF copied to the clipboard")
            else:
                self._toast("Copying requires wl-clipboard")

        self._export(dest, done, gif=True)


def Gio_file(path):
    from gi.repository import Gio
    return Gio.File.new_for_path(str(path))


def open_folder(path):
    from gi.repository import Gio
    Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(str(path)), None)


def copy_file_to_clipboard(path):
    """wl-copy keeps serving the clipboard after this app closes."""
    uri = GLib.filename_to_uri(str(path))
    try:
        subprocess.run(["wl-copy", "--type", "text/uri-list"], input=uri + "\r\n",
                       text=True, timeout=5, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
