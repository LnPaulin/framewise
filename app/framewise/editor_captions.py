"""The editor's Captions panel: make captions from the speech, fix them, style them.

Captions come from the speech model's transcript (the same one that finds
filler words, kept in the take folder). Each line can be corrected or
removed; size and position apply to all. They show in the preview and are
burned into the saved video while "Show on the video" is on, and can be
saved as an .srt file for YouTube and video players.
"""

import json

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import captions  # noqa: E402
from . import edit_model as em  # noqa: E402
from . import speech  # noqa: E402
from .editor_export import format_length  # noqa: E402

SIZES = [("small", "Small"), ("medium", "Medium"), ("large", "Large")]
POSITIONS = [("bottom", "Bottom"), ("top", "Top")]


class CaptionsMixin:
    def _build_captions_button(self):
        self._captions_busy = False
        self._captions_syncing = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12, width_request=380)
        box.append(Gtk.Label(label="Captions", xalign=0, css_classes=["heading"]))
        self.cap_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=44, css_classes=["dim-label"])
        box.append(self.cap_status)
        self.cap_progress = Gtk.ProgressBar(visible=False)
        box.append(self.cap_progress)
        self.cap_make = Gtk.Button(label="Make Captions", css_classes=["suggested-action"], halign=Gtk.Align.START)
        self.cap_make.connect("clicked", lambda *_: self._make_captions())
        box.append(self.cap_make)

        self.cap_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        show = Gtk.Box(spacing=8)
        show.append(Gtk.Label(label="Show on the video", hexpand=True, xalign=0))
        self.cap_on = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.cap_on.connect("notify::active", lambda *_: self._captions_set(on=self.cap_on.get_active()))
        show.append(self.cap_on)
        self.cap_controls.append(show)
        looks = Gtk.Box(spacing=8)
        self.cap_size = Gtk.DropDown.new_from_strings([label for _k, label in SIZES])
        self.cap_size.connect("notify::selected",
                              lambda d, _p: self._captions_set(size=SIZES[d.get_selected()][0]))
        self.cap_pos = Gtk.DropDown.new_from_strings([label for _k, label in POSITIONS])
        self.cap_pos.connect("notify::selected",
                             lambda d, _p: self._captions_set(position=POSITIONS[d.get_selected()][0]))
        looks.append(Gtk.Label(label="Size", css_classes=["dim-label"]))
        looks.append(self.cap_size)
        looks.append(Gtk.Label(label="Place", css_classes=["dim-label"], margin_start=8))
        looks.append(self.cap_pos)
        self.cap_controls.append(looks)
        self.cap_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        scroller = Gtk.ScrolledWindow(child=self.cap_list, min_content_height=120, max_content_height=300,
                                      propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.cap_controls.append(scroller)
        actions = Gtk.Box(spacing=8)
        redo = Gtk.Button(label="Make Again", tooltip_text="Replace these captions with new ones")
        redo.connect("clicked", lambda *_: self._make_captions(again=True))
        srt = Gtk.Button(label="Save Captions…", tooltip_text="Save as an .srt file (for YouTube and players)")
        srt.connect("clicked", lambda *_: self._save_srt())
        actions.append(redo)
        actions.append(srt)
        self.cap_controls.append(actions)
        box.append(self.cap_controls)

        popover = Gtk.Popover(child=box)
        popover.connect("show", lambda *_: self._refresh_captions())
        self.captions_popover = popover
        button = Gtk.MenuButton(popover=popover, tooltip_text="Captions")
        button.set_child(self._icon_label("media-view-subtitles-symbolic", "Captions"))
        return button

    # ---- showing ---------------------------------------------------------------------

    def _refresh_captions(self):
        cap = self.doc["captions"]
        items = cap["items"]
        self._captions_syncing = True
        self.cap_on.set_active(cap["on"])
        self.cap_size.set_selected([k for k, _l in SIZES].index(cap["size"]))
        self.cap_pos.set_selected([k for k, _l in POSITIONS].index(cap["position"]))
        self._captions_syncing = False
        self.cap_controls.set_visible(bool(items))
        self.cap_make.set_visible(not items)
        self.cap_make.set_sensitive(not self._captions_busy)
        if not self._captions_busy:
            if items:
                self.cap_status.set_label(f"{len(items)} lines. Click a time to jump there; edit the text "
                                          "to fix a word.")
            elif self._speech_track() is None:
                self.cap_status.set_label("This recording has no sound to make captions from.")
                self.cap_make.set_sensitive(False)
            else:
                self.cap_status.set_label("Captions are made from what is said, on this computer. "
                                          "You can fix any word afterwards.")
        self._fill_caption_rows()

    def _fill_caption_rows(self):
        # A text box that has the cursor can't be destroyed safely: move it off first.
        focus = self.get_focus()
        if focus is not None and focus.is_ancestor(self.cap_list):
            self.set_focus(None)
        while (row := self.cap_list.get_first_child()) is not None:
            self.cap_list.remove(row)
        for item in self.doc["captions"]["items"]:
            row = Gtk.Box(spacing=6, margin_top=4, margin_bottom=4, margin_start=6, margin_end=6)
            at = Gtk.Button(label=format_length(item["start"]), css_classes=["flat", "caption"],
                            tooltip_text="Jump here")
            at.connect("clicked", lambda *_a, t=item["start"]: self.player.seek(t + 0.01))
            entry = Gtk.Entry(text=item["text"], hexpand=True)
            entry.committed_text = item["text"]     # what the editor has (see the editor's Ctrl+Z)
            entry.connect("activate", lambda e, i=item["id"]: self._caption_entry_done(i, e))
            focus = Gtk.EventControllerFocus()
            focus.connect("leave", lambda _c, e=entry, i=item["id"]: self._caption_entry_done(i, e))
            entry.add_controller(focus)
            remove = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], tooltip_text="Remove")
            remove.connect("clicked", lambda *_a, i=item["id"]: self._caption_remove(i))
            row.append(at)
            row.append(entry)
            row.append(remove)
            self.cap_list.append(row)

    # ---- changes -----------------------------------------------------------------------

    def _captions_set(self, **changes):
        if self._captions_syncing:
            return
        doc = em.clone(self.doc)
        doc["captions"].update(changes)
        self._commit(doc)

    def _caption_entry_done(self, item_id, entry):
        # Only new typing: leaving a line that's being removed (undo redraws
        # the lines) mustn't write its old text back.
        if entry.get_text() == entry.committed_text:
            return
        entry.committed_text = entry.get_text()
        self._caption_text(item_id, entry.get_text())

    def _caption_text(self, item_id, text):
        doc = em.clone(self.doc)
        for c in doc["captions"]["items"]:
            if c["id"] == item_id and c["text"] != " ".join(text.split()):
                c["text"] = text
                self._commit(doc)       # an empty line is removed by normalize
                if not text.strip():
                    self._refresh_captions()
                return

    def _caption_remove(self, item_id):
        doc = em.clone(self.doc)
        doc["captions"]["items"] = [c for c in doc["captions"]["items"] if c["id"] != item_id]
        self._commit(doc)
        self._refresh_captions()

    # ---- making them -----------------------------------------------------------------

    def _speech_track(self):
        for name in ("mic", "system"):
            if name in self.tracks:
                return name
        return None

    def _make_captions(self, again=False):
        track = self._speech_track()
        if track is None or self._captions_busy:
            return
        cache = self.take_dir / ("transcript.json" if track == "mic" else f"transcript-{track}.json")
        if cache.exists() and not again:
            try:
                self._captions_from(json.loads(cache.read_text()).get("words") or [])
                return
            except (OSError, ValueError):
                pass
        if not speech.is_ready():
            self.captions_popover.popdown()
            self._offer_speech_for_captions()
            return
        self._captions_busy = True
        self.cap_make.set_sensitive(False)
        self.cap_progress.set_visible(True)
        self.cap_progress.set_fraction(0)
        self.cap_status.set_label("Listening to the recording…")

        def progress(f):
            self.cap_progress.set_fraction(f)
            self.cap_status.set_label(f"Listening to the recording… {round(f * 100)} %")

        def done(words, message):
            self._captions_busy = False
            self.cap_progress.set_visible(False)
            if words is None:
                self.cap_status.set_label(f"Couldn't listen to the recording: {message[-200:]}")
                self.cap_make.set_sensitive(True)
                return
            if track == "mic":
                self.filler_words = words        # the filler finder can use it too
            self._captions_from(words)

        speech.transcribe_async(self.take, self.tracks.index(track), cache, progress, done)

    def _captions_from(self, words):
        items = captions.build(words)
        if not items:
            self.cap_status.set_label("No speech was found in this recording.")
            self.cap_make.set_sensitive(True)
            return
        doc = em.clone(self.doc)
        doc["captions"]["items"] = items
        doc["captions"]["on"] = True
        self._commit(doc)
        self._refresh_captions()
        self._toast(f"{len(items)} caption lines made")

    def _offer_speech_for_captions(self):
        if not speech.can_install():
            self._toast("Captions need the python3-venv package: sudo apt install python3-venv")
            return
        dialog = Adw.AlertDialog(
            heading="Download speech recognition?",
            body=f"To make captions, the editor needs speech recognition ({speech.DOWNLOAD_SIZE}, "
                 "downloaded once). It runs on this computer: your recordings are never uploaded.")
        dialog.add_response("cancel", "Not Now")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)

        def answer(_d, response):
            if response != "download":
                return
            toast = Adw.Toast(title="Downloading speech recognition…", timeout=0)
            self.toasts.add_toast(toast)

            def done(ok, message):
                toast.dismiss()
                if ok:
                    self._toast("Speech recognition is ready. Making captions…")
                    self._make_captions()
                else:
                    self._toast("Speech recognition couldn't be installed")
            speech.install_async(lambda label, _f: toast.set_title(f"{label}…"), done)
        dialog.connect("response", answer)
        dialog.present(self)

    # ---- .srt --------------------------------------------------------------------------

    def _save_srt(self):
        text = captions.to_srt(self.doc)
        if not text:
            self._toast("No captions in the kept parts")
            return
        dialog = Gtk.FileDialog(title="Save Captions", initial_name=f"{self.title.get_title() or 'Recording'}.srt")
        videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
        if videos:
            dialog.set_initial_folder(Gio.File.new_for_path(videos))

        def done(d, result):
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return
            try:
                file.replace_contents(text.encode("utf-8"), None, False, Gio.FileCreateFlags.NONE, None)
                self._toast(f"Saved {file.get_basename()}")
            except GLib.Error as e:
                self._toast(f"Couldn't save: {e.message}")
        dialog.save(self, None, done)
