"""The "Make a Video Fit" window: choose a video and a size, get a copy that fits."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import fit  # noqa: E402
from .state import settings  # noqa: E402

VIDEO_TYPES = ["video/mp4", "video/x-matroska", "video/webm", "video/quicktime", "video/x-msvideo",
               "video/mpeg", "video/3gpp", "video/x-m4v", "image/gif"]


def _length(seconds):
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class FitWindow(Adw.ApplicationWindow):
    def __init__(self, app, path=None):
        super().__init__(application=app, title="Make a Video Fit", default_width=460, resizable=False)
        self.app = app
        self.info = None
        self.job = None
        self.result = None

        header = Adw.HeaderBar()
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)

        intro = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label"], max_width_chars=50,
                          label="A copy of a video, small enough to send in a chat or by email. "
                                "The original stays as it is.")
        page.append(intro)

        files = Adw.PreferencesGroup()
        self.file_row = Adw.ActionRow(title="No video chosen", subtitle="Choose one, or drop it here")
        self.file_row.add_prefix(Gtk.Image(icon_name="video-x-generic-symbolic"))
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose())
        self.file_row.add_suffix(choose)
        files.add(self.file_row)
        page.append(files)

        sizes = Adw.PreferencesGroup(title="Size")
        self.size_buttons = []
        group = None
        last = float(settings.get("fit_target_mb") or 10)
        for mb, label in fit.PRESETS:
            row = Adw.ActionRow(title=f"{mb} MB", subtitle=label)
            check = Gtk.CheckButton(group=group, valign=Gtk.Align.CENTER)
            group = group or check
            row.add_prefix(check)
            row.set_activatable_widget(check)
            check.connect("toggled", lambda *_: self._update())
            self.size_buttons.append((mb, check))
            sizes.add(row)
        custom = Adw.ActionRow(title="Other size")
        self.custom_check = Gtk.CheckButton(group=group, valign=Gtk.Align.CENTER)
        custom.add_prefix(self.custom_check)
        custom.set_activatable_widget(self.custom_check)
        self.custom_mb = Gtk.SpinButton.new_with_range(1, 4000, 1)
        self.custom_mb.set_valign(Gtk.Align.CENTER)
        self.custom_mb.set_value(last if last not in [p[0] for p in fit.PRESETS] else 8)
        self.custom_mb.connect("value-changed", lambda *_: (self.custom_check.set_active(True), self._update()))
        custom.add_suffix(self.custom_mb)
        custom.add_suffix(Gtk.Label(label="MB", css_classes=["dim-label"]))
        self.custom_check.connect("toggled", lambda *_: self._update())
        sizes.add(custom)
        page.append(sizes)

        self.note = Gtk.Label(wrap=True, xalign=0, max_width_chars=50, css_classes=["dim-label"])
        page.append(self.note)
        self.progress = Gtk.ProgressBar(visible=False, show_text=True)
        page.append(self.progress)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.cancel_button = Gtk.Button(label="Cancel", visible=False)
        self.cancel_button.connect("clicked", lambda *_: self._cancel())
        self.folder_button = Gtk.Button(label="Show in Folder", visible=False)
        self.folder_button.connect("clicked", lambda *_: self._show_folder())
        self.copy_button = Gtk.Button(label="Copy", visible=False,
                                      tooltip_text="Copy the file, to paste it into a chat or an email")
        self.copy_button.connect("clicked", lambda *_: self._copy())
        self.go = Gtk.Button(label="Make It Fit", css_classes=["suggested-action"], sensitive=False)
        self.go.connect("clicked", lambda *_: self._start())
        for b in (self.cancel_button, self.folder_button, self.copy_button, self.go):
            buttons.append(b)
        page.append(buttons)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self.set_content(view)

        # The last size used (now that every widget exists).
        for mb, check in self.size_buttons:
            if mb == last:
                check.set_active(True)
        if not any(c.get_active() for _mb, c in self.size_buttons):
            self.custom_check.set_active(True)

        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, value, *_: (self.load(value.get_path()), True)[1])
        self.add_controller(drop)
        self.connect("close-request", lambda *_: self._cancel() or False)

        if path:
            self.load(path)

    # ---- choosing ------------------------------------------------------------------

    def target_mb(self):
        if self.custom_check.get_active():
            return float(self.custom_mb.get_value())
        return next((mb for mb, c in self.size_buttons if c.get_active()), 10)

    def _choose(self):
        dialog = Gtk.FileDialog(title="Choose a Video")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="Videos")
        for mime in VIDEO_TYPES:
            f.add_mime_type(mime)
        filters.append(f)
        dialog.set_filters(filters)
        videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
        if videos:
            dialog.set_initial_folder(Gio.File.new_for_path(videos))

        def done(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            if file and file.get_path():
                self.load(file.get_path())
        dialog.open(self, None, done)

    def load(self, path):
        if self.job:
            return
        try:
            self.info = fit.Info(path)
        except (OSError, ValueError, KeyError, TypeError, __import__("subprocess").SubprocessError) as e:
            self.info = None
            self.file_row.set_title(GLib.markup_escape_text(GLib.path_get_basename(str(path))))
            self.file_row.set_subtitle(f"Can't use this file: {e}" if str(e) else "Can't use this file")
            self._update()
            return
        i = self.info
        self.file_row.set_title(GLib.markup_escape_text(i.path.name))
        self.file_row.set_subtitle(f"{i.megabytes:.1f} MB · {_length(i.duration)} · {i.width} × {i.height}")
        self._reset_result()
        self._update()

    def _update(self):
        busy = self.job is not None
        mb = self.target_mb()
        self.go.set_sensitive(bool(self.info) and not busy)
        if not self.info or busy:
            self.note.set_text("")
            return
        if self.info.megabytes <= mb:
            self.note.set_text(f"This video is already under {mb:g} MB; a copy would only lose quality.")
            self.go.set_sensitive(False)
            return
        from . import export
        plan = export.fit_plan(self.info.duration, (self.info.width, self.info.height), self.info.fps,
                               self.info.has_audio, mb)
        if plan is None:
            self.note.set_text(fit.too_long_message(self.info, mb))
            self.go.set_sensitive(False)
            return
        w, h = plan["size"]
        self.note.set_text(f"The copy will be under {mb:g} MB, at least {w} × {h} "
                           f"and {plan['fps']:g} frames a second.")

    # ---- making it ---------------------------------------------------------------------

    def _start(self):
        if not self.info or self.job:
            return
        mb = self.target_mb()
        settings.set("fit_target_mb", mb)
        self._reset_result()
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.progress.set_text("Making the copy…")
        self.cancel_button.set_visible(True)
        self.app.hold()
        self.job = fit.fit_async(self.info, mb, self._progress, self._done)
        self._update()

    def _progress(self, fraction):
        self.progress.set_fraction(fraction)
        self.progress.set_text(f"Making the copy… {int(fraction * 100)}%")

    def _done(self, ok, message):
        self.job = None
        self.app.release()
        self.cancel_button.set_visible(False)
        if ok:
            from pathlib import Path
            self.result = Path(message)
            size = self.result.stat().st_size / 1_000_000
            self.progress.set_fraction(1)
            self.progress.set_text(f"Saved: {self.result.name} ({size:.1f} MB)")
            self.folder_button.set_visible(True)
            self.copy_button.set_visible(True)
        elif message == "cancelled":
            self.progress.set_visible(False)
        else:
            self.progress.set_visible(False)
            self.note.set_text(message.splitlines()[-1] if message else "The copy couldn't be made")
            self.go.set_sensitive(True)
            return
        self._update()

    def _cancel(self):
        if self.job:
            self.job.cancel()

    def _reset_result(self):
        self.result = None
        self.folder_button.set_visible(False)
        self.copy_button.set_visible(False)
        self.progress.set_visible(False)

    def _show_folder(self):
        if self.result:
            Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.result))).open_containing_folder(self, None, None)

    def _copy(self):
        from .editor_export import copy_file_to_clipboard
        if self.result and copy_file_to_clipboard(self.result):
            self.copy_button.set_label("Copied")
            GLib.timeout_add(1500, lambda: (self.copy_button.set_label("Copy"), False)[1])
