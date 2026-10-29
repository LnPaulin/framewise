"""The "PDF Tools" window: a list of files, and what can be done with them."""

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import pdf  # noqa: E402


def _size(n):
    return GLib.format_size(n)


class PdfWindow(Adw.ApplicationWindow):
    def __init__(self, app, paths=None):
        super().__init__(application=app, title="PDF Tools", default_width=560, default_height=600)
        self.app = app
        self.files = []          # [(Path, pages or None)]
        self.busy = False
        self.result = None

        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Files")
        add.connect("clicked", lambda *_: self._choose())
        header.pack_start(add)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)

        self.empty = Adw.StatusPage(icon_name="x-office-document-symbolic", title="Drop PDFs or pictures here",
                                    description="Merge, pick pages, compress or rotate PDFs, and turn "
                                                "pictures into a PDF. The originals are never changed.",
                                    vexpand=True)
        choose = Gtk.Button(label="Choose Files…", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        choose.connect("clicked", lambda *_: self._choose())
        self.empty.set_child(choose)
        page.append(self.empty)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.scroller = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, max_content_height=300)
        page.append(self.scroller)

        # What can be done (enabled to suit the files).
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, min_children_per_line=2,
                           column_spacing=8, row_spacing=8, homogeneous=True)
        self.actions = {}

        def action(key, label, icon, handler, tip):
            content = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
            content.append(Gtk.Image(icon_name=icon))
            content.append(Gtk.Label(label=label))
            button = Gtk.Button(child=content, tooltip_text=tip)
            button.connect("clicked", lambda *_: handler())
            self.actions[key] = button
            grid.append(button)

        action("merge", "Merge", "list-add-symbolic", self._merge, "Join the files into one PDF, in this order")
        action("pages", "Pick Pages", "edit-select-all-symbolic", self._pages_ask,
               "Keep some pages as a new PDF (e.g. 1-3, 7)")
        action("split", "Split", "edit-cut-symbolic", self._split, "Every page as its own PDF")
        action("compress", "Compress", "zoom-out-symbolic", self._compress_ask, "Make the PDF smaller")
        action("rotate", "Rotate", "object-rotate-right-symbolic", self._rotate_ask, "Turn every page")
        action("images", "To Pictures", "image-x-generic-symbolic", self._to_images,
               "Every page as a PNG picture")
        action("topdf", "Make a PDF", "x-office-document-symbolic", self._images_to_pdf,
               "The pictures as a PDF, one per page")
        page.append(grid)
        self.grid = grid

        self.status = Gtk.Label(wrap=True, xalign=0, max_width_chars=60, css_classes=["dim-label"])
        # Adw.Spinner is libadwaita 1.6+ (Ubuntu 24.04 has 1.5).
        self.spinner = (Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True))
        self.spinner.set_visible(False)
        result = Gtk.Box(spacing=8)
        result.append(self.spinner)
        result.append(self.status)
        self.status.set_hexpand(True)
        self.show_button = Gtk.Button(label="Show in Folder", visible=False)
        self.show_button.connect("clicked", lambda *_: self._show_result())
        result.append(self.show_button)
        page.append(result)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, value, *_: (self.add_paths([f.get_path() for f in value.get_files()
                                                                     if f.get_path()]), True)[1])
        self.add_controller(drop)
        self._update()
        if not pdf.available():
            self.empty.set_description("PDF Tools need two Ubuntu packages. In a terminal, run:\n"
                                       "sudo apt install poppler-utils ghostscript")
            choose.set_sensitive(False)
            return
        if paths:
            self.add_paths(paths)

    # ---- files ---------------------------------------------------------------------------

    def _choose(self):
        dialog = Gtk.FileDialog(title="Add PDFs or Pictures")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter(name="PDFs and pictures")
        f.add_mime_type("application/pdf")
        f.add_pixbuf_formats()
        filters.append(f)
        dialog.set_filters(filters)

        def done(d, result):
            try:
                files = d.open_multiple_finish(result)
            except GLib.Error:
                return
            self.add_paths([f.get_path() for f in files if f.get_path()])
        dialog.open_multiple(self, None, done)

    def add_paths(self, paths):
        skipped = 0
        for p in paths:
            p = Path(p)
            if not p.is_file() or not (pdf.is_pdf(p) or pdf.is_image(p)):
                skipped += 1
                continue
            pages = None
            if pdf.is_pdf(p):
                try:
                    pages = pdf.page_count(p)
                except pdf.PdfError as e:
                    self._toast(f"{p.name}: {e}")
                    continue
            self.files.append((p, pages))
        if skipped:
            self._toast("Only PDFs and pictures can be added")
        self._fill()

    def _fill(self):
        while (row := self.list.get_first_child()) is not None:
            self.list.remove(row)
        for i, (path, pages) in enumerate(self.files):
            row = Adw.ActionRow(title=GLib.markup_escape_text(path.name),
                                subtitle=(f"{pages} page{'s' if pages != 1 else ''} · " if pages else "Picture · ")
                                + _size(path.stat().st_size))
            row.add_prefix(Gtk.Image(icon_name="x-office-document-symbolic" if pages else "image-x-generic-symbolic"))
            for icon, tip, move in (("go-up-symbolic", "Move up", -1), ("go-down-symbolic", "Move down", 1)):
                b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER, css_classes=["flat"],
                               sensitive=0 <= i + move < len(self.files))
                b.connect("clicked", lambda *_a, i=i, m=move: self._move(i, m))
                row.add_suffix(b)
            remove = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Remove from the list",
                                valign=Gtk.Align.CENTER, css_classes=["flat"])
            remove.connect("clicked", lambda *_a, i=i: self._remove(i))
            row.add_suffix(remove)
            self.list.append(row)
        self._update()

    def _move(self, i, d):
        self.files[i], self.files[i + d] = self.files[i + d], self.files[i]
        self._fill()

    def _remove(self, i):
        del self.files[i]
        self._fill()

    def _update(self):
        has = bool(self.files)
        self.empty.set_visible(not has)
        self.scroller.set_visible(has)
        self.grid.set_visible(has)
        pdfs = [p for p, n in self.files if n]
        images = [p for p, n in self.files if not n]
        one_pdf = len(self.files) == 1 and len(pdfs) == 1
        enabled = {"merge": len(self.files) >= 2, "pages": one_pdf, "split": one_pdf and self.files[0][1] > 1,
                   "compress": one_pdf, "rotate": one_pdf, "images": one_pdf,
                   "topdf": bool(images) and not pdfs}
        for key, button in self.actions.items():
            button.set_sensitive(enabled[key] and not self.busy)
        if has and not self.busy and not self.result:
            if one_pdf:
                hint = "What should happen to this PDF?"
            elif images and not pdfs:
                hint = "Make a PDF from these pictures, one per page (use the arrows to set the order)."
            else:
                hint = "Merge joins them in this order; use the arrows to change it."
            self.status.set_text(hint)

    # ---- running a tool ------------------------------------------------------------------

    def _run(self, label, work, done_text):
        """work() -> Path (file or folder); runs in the background."""
        self.busy = True
        self.result = None
        self.show_button.set_visible(False)
        self.spinner.set_visible(True)
        self.status.set_text(f"{label}…")
        self._update()
        self.app.hold()

        def thread():
            try:
                out, error = work(), None
            except pdf.PdfError as e:
                out, error = None, str(e)
            except OSError as e:
                out, error = None, str(e)
            GLib.idle_add(finished, out, error)

        def finished(out, error):
            self.busy = False
            self.spinner.set_visible(False)
            self.app.release()
            if error:
                self.status.set_text(error)
            else:
                self.result = Path(out)
                self.status.set_text(done_text(self.result))
                self.show_button.set_visible(True)
            self._update()
            self.result = None if error else self.result
            return False

        threading.Thread(target=thread, daemon=True).start()

    def _show_result(self):
        if self.result is None:
            return
        target = Gio.File.new_for_path(str(self.result))
        launcher = Gtk.FileLauncher.new(target)
        if self.result.is_dir():
            launcher.launch(self, None, None)
        else:
            launcher.open_containing_folder(self, None, None)

    def _saved(self, path):
        return f"Saved “{path.name}” ({_size(path.stat().st_size)})."

    def _merge(self):
        files = [p for p, _n in self.files]
        dest = pdf.output_path(files[0], "merged")
        self._run("Merging", lambda: pdf.merge(files, dest), self._saved)

    def _images_to_pdf(self):
        images = [p for p, _n in self.files]
        dest = pdf.output_path(images[0], "", ".pdf")
        self._run("Making the PDF", lambda: pdf.images_to_pdf(images, dest), self._saved)

    def _split(self):
        src = self.files[0][0]
        folder = pdf.output_path(src, "pages", ext="")
        self._run("Splitting", lambda: pdf.split_all(src, folder),
                  lambda f: f"Saved {self.files[0][1]} PDFs in the folder “{f.name}”.")

    def _to_images(self):
        src = self.files[0][0]
        folder = pdf.output_path(src, "pictures", ext="")
        self._run("Making pictures", lambda: pdf.to_images(src, folder),
                  lambda f: f"Saved {self.files[0][1]} pictures in the folder “{f.name}”.")

    def _pages_ask(self):
        src, count = self.files[0]
        dialog = Adw.AlertDialog(heading="Pick Pages",
                                 body=f"Which pages should the new PDF have? This one has {count}.\n"
                                      "For example: 1-3, 7 (or 5- for page 5 to the end).")
        entry = Gtk.Entry(placeholder_text=f"1-{count}", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("keep", "Make PDF")
        dialog.set_default_response("keep")
        dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)

        def answer(_d, response):
            if response != "keep":
                return
            try:
                pages = pdf.parse_pages(entry.get_text(), count)
            except pdf.PdfError as e:
                self.status.set_text(str(e))
                return
            label = entry.get_text().strip().replace(" ", "")
            dest = pdf.output_path(src, f"pages {label}")
            self._run("Picking the pages", lambda: pdf.pick_pages(src, pages, dest), self._saved)
        dialog.connect("response", answer)
        dialog.present(self)

    def _compress_ask(self):
        src = self.files[0][0]
        dialog = Adw.AlertDialog(heading="Compress",
                                 body="Pictures in the PDF are made smaller; text stays sharp.")
        dialog.add_response("cancel", "Cancel")
        for level, (_preset, label) in pdf.COMPRESS.items():
            dialog.add_response(level, label.split(" (")[0])
        dialog.set_body(dialog.get_body() + "\n\n" + "\n".join(f"• {label}" for _p, label in pdf.COMPRESS.values()))
        dialog.set_response_appearance("smaller", Adw.ResponseAppearance.SUGGESTED)

        def answer(_d, level):
            if level not in pdf.COMPRESS:
                return
            dest = pdf.output_path(src, "compressed")

            def work():
                out, _saved = pdf.compress(src, dest, level)
                return out
            before = src.stat().st_size
            self._run("Compressing", work,
                      lambda p: f"Saved “{p.name}”: {_size(p.stat().st_size)} instead of {_size(before)}.")
        dialog.connect("response", answer)
        dialog.present(self)

    def _rotate_ask(self):
        src = self.files[0][0]
        dialog = Adw.AlertDialog(heading="Rotate Every Page")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("270", "Left")
        dialog.add_response("180", "Upside Down")
        dialog.add_response("90", "Right")
        dialog.set_response_appearance("90", Adw.ResponseAppearance.SUGGESTED)

        def answer(_d, response):
            if response not in ("90", "180", "270"):
                return
            dest = pdf.output_path(src, "rotated")
            self._run("Rotating", lambda: pdf.rotate(src, dest, int(response)), self._saved)
        dialog.connect("response", answer)
        dialog.present(self)

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text))
