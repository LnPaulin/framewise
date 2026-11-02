"""The "Send to Phone" window: choose something, scan the QR code with a phone.
Or the other way: scan a code to send files and text from the phone here."""

import tempfile
import zipfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import share  # noqa: E402
from .state import settings  # noqa: E402


class SendWindow(Adw.ApplicationWindow):
    def __init__(self, app, paths=None, receive=False):
        super().__init__(application=app, title="Send to Phone", default_width=420, resizable=False)
        self.app = app
        self.sharing = None
        self.receiving = None
        self._temp = tempfile.TemporaryDirectory(prefix="framewise-send-")
        self._ticker = 0

        header = Adw.HeaderBar()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)

        # ---- choosing
        choose = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        choose.append(Gtk.Label(wrap=True, xalign=0, max_width_chars=46, css_classes=["dim-label"],
                                label="Scan a QR code with your phone to get a file, a picture or text. "
                                      "Your phone must be on the same Wi-Fi. Nothing goes through the internet."))
        group = Adw.PreferencesGroup()
        for title, subtitle, icon, handler in (
                ("A File", "Or drop files on this window", "document-open-symbolic", self._choose_file),
                ("The Clipboard", "Text or a picture you copied", "edit-paste-symbolic", self._from_clipboard),
                ("The Latest Screenshot", None, "applets-screenshooter-symbolic", self._latest_shot)):
            row = Adw.ActionRow(title=title, subtitle=subtitle or "", activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda *_a, h=handler: h())
            group.add(row)
        choose.append(group)
        back = Adw.PreferencesGroup(title="From Your Phone")
        row = Adw.ActionRow(title="Get Files from Your Phone", subtitle="Photos, videos, any file, or text",
                            activatable=True)
        row.add_prefix(Gtk.Image(icon_name="folder-download-symbolic"))
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        row.connect("activated", lambda *_: self._start_receiving())
        back.add(row)
        choose.append(back)
        self.choose_note = Gtk.Label(wrap=True, xalign=0, max_width_chars=46, css_classes=["error"])
        choose.append(self.choose_note)
        self.stack.add_named(choose, "choose")

        # ---- sharing
        show = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=6, margin_bottom=18, margin_start=18, margin_end=18)
        self.qr = Gtk.Picture(can_shrink=False, halign=Gtk.Align.CENTER, css_classes=["card"])
        self.qr.set_size_request(260, 260)
        show.append(self.qr)
        self.what = Gtk.Label(wrap=True, max_width_chars=40, css_classes=["heading"], margin_top=6)
        show.append(self.what)
        self.status = Gtk.Label(wrap=True, max_width_chars=44, css_classes=["dim-label"])
        show.append(self.status)
        self.link = Gtk.Label(selectable=True, wrap=True, wrap_mode=2, max_width_chars=44,
                              css_classes=["caption", "dim-label"])
        show.append(self.link)
        # The laptop can't tell why a phone can't reach it; these are the usual reasons.
        self.help = Gtk.Expander(label="Page won't open on your phone?", halign=Gtk.Align.CENTER)
        self.help.set_child(Gtk.Label(
            wrap=True, xalign=0, max_width_chars=44, css_classes=["caption"], margin_top=6,
            label="• Turn off any VPN on the phone: it keeps the phone from reaching this computer.\n"
                  "• Use the same Wi-Fi as this computer, not a guest network. Some routers keep "
                  "devices apart (\u201cclient isolation\u201d or \u201cAP isolation\u201d in the "
                  "router\u2019s settings).\n"
                  "• Type the address under the code into the phone\u2019s browser yourself."))
        show.append(self.help)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        other = Gtk.Button(label="Send Something Else")
        other.connect("clicked", lambda *_: self._stop_and_choose())
        done = Gtk.Button(label="Done", css_classes=["suggested-action"])
        done.connect("clicked", lambda *_: self.close())
        buttons.append(other)
        buttons.append(done)
        show.append(buttons)
        self.stack.add_named(show, "show")
        self.stack.add_named(self._build_receive(), "receive")

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.set_content(view)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, value, *_: (self.send_paths([f.get_path() for f in value.get_files()
                                                                      if f.get_path()]), True)[1])
        self.add_controller(drop)
        self.connect("close-request", self._on_close)

        if paths:
            self.send_paths(paths)
        elif receive:
            self._start_receiving()

    # ---- what to send ----------------------------------------------------------------

    def _choose_file(self):
        dialog = Gtk.FileDialog(title="Choose What to Send")

        def done(d, result):
            try:
                files = d.open_multiple_finish(result)
            except GLib.Error:
                return
            self.send_paths([f.get_path() for f in files if f.get_path()])
        dialog.open_multiple(self, None, done)

    def send_paths(self, paths):
        paths = [Path(p) for p in paths if Path(p).is_file()]
        if not paths:
            self.choose_note.set_text("Only files can be sent (not folders).")
            return
        if len(paths) == 1:
            self._start(share.Item(path=paths[0]))
            return
        # Several files go as one zip.
        archive = Path(self._temp.name) / f"{len(paths)} files from Framewise.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            used = set()
            for p in paths:
                name, n = p.name, 2
                while name in used:
                    name = f"{p.stem} {n}{p.suffix}"
                    n += 1
                used.add(name)
                z.write(p, name)
        self._start(share.Item(path=archive))

    def _from_clipboard(self):
        clipboard = Gdk.Display.get_default().get_clipboard()
        formats = clipboard.get_formats()
        if formats.contain_gtype(Gdk.Texture) or any(m.startswith("image/") for m in formats.get_mime_types() or []):
            def got_texture(c, result):
                try:
                    texture = c.read_texture_finish(result)
                except GLib.Error:
                    texture = None
                if texture is None:
                    self.choose_note.set_text("The picture on the clipboard couldn't be read.")
                    return
                path = Path(self._temp.name) / "Picture.png"
                texture.save_to_png(str(path))
                self._start(share.Item(path=path))
            clipboard.read_texture_async(None, got_texture)
            return

        def got_text(c, result):
            try:
                text = c.read_text_finish(result)
            except GLib.Error:
                text = None
            if not text:
                self.choose_note.set_text("The clipboard is empty. Copy some text or a picture first.")
                return
            uri = text.strip()
            if uri.startswith("file://") and "\n" not in uri:
                path = Gio.File.new_for_uri(uri).get_path()
                if path and Path(path).is_file():
                    self._start(share.Item(path=path))      # a file copied in Files
                    return
            self._start(share.Item(text=text, name="Text from the clipboard"))
        clipboard.read_text_async(None, got_text)

    def _latest_shot(self):
        shots = getattr(self.app, "shots", None)
        latest = shots.last if shots is not None and shots.last and Path(shots.last).exists() else None
        if latest is None:
            folder = Path(settings.get("shot_folder") or Path.home() / "Pictures" / "Screenshots").expanduser()
            pictures = sorted((p for p in folder.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")),
                              key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
            latest = pictures[0] if pictures else None
        if latest is None:
            self.choose_note.set_text("No screenshot found yet.")
            return
        self._start(share.Item(path=latest))

    # ---- sharing ---------------------------------------------------------------------

    def _start(self, item):
        self._stop()
        try:
            self.sharing = share.Sharing(item, self._on_event)
        except OSError as e:
            self.choose_note.set_text(f"Couldn't start sharing: {e}")
            return
        self.choose_note.set_text("")
        texture = share.qr_texture(self.sharing.url)
        self.qr.set_paintable(texture)
        self.qr.set_visible(texture is not None)
        self.what.set_text(f"{item.name} · {share.human_size(item.size)}" if item.path else item.name)
        self.link.set_text(self.sharing.url if texture else f"Open this on your phone: {self.sharing.url}")
        self._set_status("Scan the code with your phone's camera.")
        self.help.set_visible(True)
        self.help.set_expanded(False)
        self.stack.set_visible_child_name("show")
        self.app.hold()
        self._ticker = GLib.timeout_add_seconds(30, self._tick)

    def _set_status(self, text):
        self._status_text = text
        self._tick()

    def _tick(self):
        if not self.sharing or self.sharing.stopped:
            return False
        import time
        left = max(0, int((self.sharing.expires - time.monotonic()) / 60 + 0.99))
        self.status.set_text(f"{self._status_text}\nWorks for {left} more minute{'s' if left != 1 else ''}.")
        return True

    def _on_event(self, kind, detail):
        if self.sharing is None:
            return
        if kind == "opened":
            self._set_status("Opened on your phone.")
            self.help.set_visible(False)
        elif kind == "downloaded":
            self._set_status("Sent to your phone ✓")
        elif kind == "stopped":
            self.status.set_text("This code has expired. Choose “Send Something Else” for a new one.")
            self.qr.set_opacity(0.25)

    def _stop(self):
        if self.receiving is not None:
            receiving, self.receiving = self.receiving, None
            receiving.on_event = None
            receiving.stop()
            self.app.release()
        if self._ticker:
            GLib.source_remove(self._ticker)
            self._ticker = 0
        if self.sharing is not None:
            sharing, self.sharing = self.sharing, None
            sharing.on_event = None
            sharing.stop()
            self.qr.set_opacity(1)
            self.app.release()

    def _stop_and_choose(self):
        self._stop()
        self.set_title("Send to Phone")
        self.stack.set_visible_child_name("choose")

    # ---- receiving from the phone ----------------------------------------------------

    def _build_receive(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=6, margin_bottom=18, margin_start=18, margin_end=18)
        self.rqr = Gtk.Picture(can_shrink=False, halign=Gtk.Align.CENTER, css_classes=["card"])
        self.rqr.set_size_request(260, 260)
        page.append(self.rqr)
        page.append(Gtk.Label(label="Scan with your phone to send files here", wrap=True,
                              max_width_chars=40, css_classes=["heading"], margin_top=6))
        self.rstatus = Gtk.Label(wrap=True, max_width_chars=44, css_classes=["dim-label"])
        page.append(self.rstatus)
        self.rlink = Gtk.Label(selectable=True, wrap=True, wrap_mode=2, max_width_chars=44,
                               css_classes=["caption", "dim-label"])
        page.append(self.rlink)
        self.rhelp = Gtk.Expander(label="Page won't open on your phone?", halign=Gtk.Align.CENTER)
        self.rhelp.set_child(Gtk.Label(
            wrap=True, xalign=0, max_width_chars=44, css_classes=["caption"], margin_top=6,
            label="• Turn off any VPN or ad-blocking app on the phone: it keeps the phone from "
                  "reaching this computer.\n"
                  "• Use the same Wi-Fi as this computer, not a guest network.\n"
                  "• Type the address under the code into the phone\u2019s browser yourself."))
        page.append(self.rhelp)
        folder = Adw.ActionRow(title="Saved in")
        self.rfolder = folder
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER, css_classes=["flat"])
        change.connect("clicked", lambda *_: self._choose_folder())
        folder.add_suffix(change)
        where = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        where.append(folder)
        page.append(where)
        self.rlist = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                 visible=False)
        scroller = Gtk.ScrolledWindow(child=self.rlist, propagate_natural_height=True,
                                      max_content_height=240, hscrollbar_policy=Gtk.PolicyType.NEVER)
        page.append(scroller)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        self.ragain = Gtk.Button(label="New Code", visible=False)
        self.ragain.connect("clicked", lambda *_: self._start_receiving())
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda *_: self._stop_and_choose())
        done = Gtk.Button(label="Done", css_classes=["suggested-action"])
        done.connect("clicked", lambda *_: self.close())
        for b in (self.ragain, back, done):
            buttons.append(b)
        page.append(buttons)
        return page

    def _receive_folder(self):
        chosen = settings.get("receive_folder")
        if chosen:
            return Path(chosen).expanduser()
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        return Path(downloads) if downloads else Path.home() / "Downloads"

    def _show_folder(self):
        folder = self._receive_folder()
        home = Path.home()
        shown = "~/" + str(folder.relative_to(home)) if folder.is_relative_to(home) else str(folder)
        self.rfolder.set_subtitle(shown)

    def _choose_folder(self):
        dialog = Gtk.FileDialog(title="Where Files from Your Phone Go",
                                initial_folder=Gio.File.new_for_path(str(self._receive_folder())))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                settings.set("receive_folder", folder.get_path())
                self._show_folder()
                if self.receiving is not None:
                    self.receiving.folder = Path(folder.get_path())
        dialog.select_folder(self, None, done)

    def _start_receiving(self):
        self._stop()
        try:
            self.receiving = share.Receiving(self._receive_folder(), self._on_receive_event)
        except OSError as e:
            self.choose_note.set_text(f"Couldn't start: {e}")
            self.stack.set_visible_child_name("choose")
            return
        self.choose_note.set_text("")
        texture = share.qr_texture(self.receiving.url)
        self.rqr.set_paintable(texture)
        self.rqr.set_visible(texture is not None)
        self.rqr.set_opacity(1)
        self.rlink.set_text(self.receiving.url if texture else f"Open this on your phone: {self.receiving.url}")
        self._show_folder()
        self._received = 0
        self.rhelp.set_visible(True)
        self.rhelp.set_expanded(False)
        self.ragain.set_visible(False)
        self.rstatus.set_text("Scan the code with your phone's camera. Your phone must be on the same Wi-Fi.")
        self.stack.set_visible_child_name("receive")
        self.set_title("Get Files from Your Phone")
        self.app.hold()

    def _on_receive_event(self, kind, detail):
        if self.receiving is None:
            return
        if kind == "opened":
            self.rhelp.set_visible(False)
            if not self._received:
                self.rstatus.set_text("Opened on your phone. Choose files there, or send some text.")
        elif kind == "receiving":
            self.rstatus.set_text(f"Receiving {detail}…")
        elif kind == "received":
            self._received += 1
            self.rstatus.set_text(f"Received {self._received} file{'s' if self._received != 1 else ''}.")
            self._add_file_row(Path(detail))
        elif kind == "failed":
            self.rstatus.set_text(f"{detail} didn't arrive whole. Try sending it again.")
        elif kind == "text":
            Gdk.Display.get_default().get_clipboard().set(detail)
            self.rstatus.set_text("Text received, and copied: paste it anywhere.")
            self._add_text_row(detail)
        elif kind == "stopped":
            self.rstatus.set_text("This code has stopped working after a while without use.")
            self.rqr.set_opacity(0.25)
            self.ragain.set_visible(True)

    def _add_file_row(self, path):
        row = Adw.ActionRow(title=GLib.markup_escape_text(path.name), subtitle=share.human_size(path.stat().st_size)
                            if path.exists() else "")
        row.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic"))
        show = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Show in Folder",
                          valign=Gtk.Align.CENTER, css_classes=["flat"])
        show.connect("clicked", lambda *_: Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
                     .open_containing_folder(self, None, None))
        row.add_suffix(show)
        open_ = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        open_.connect("clicked", lambda *_: Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
                      .launch(self, None, None))
        row.add_suffix(open_)
        self.rlist.prepend(row)
        self.rlist.set_visible(True)

    def _add_text_row(self, text):
        line = " ".join(text.split())
        row = Adw.ActionRow(title=GLib.markup_escape_text(line[:80] + ("…" if len(line) > 80 else "")),
                            subtitle="Text · copied")
        row.add_prefix(Gtk.Image(icon_name="edit-paste-symbolic"))
        copy = Gtk.Button(label="Copy", valign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda *_: Gdk.Display.get_default().get_clipboard().set(text))
        row.add_suffix(copy)
        self.rlist.prepend(row)
        self.rlist.set_visible(True)

    def _on_close(self, *_):
        self._stop()
        self._temp.cleanup()
        return False
