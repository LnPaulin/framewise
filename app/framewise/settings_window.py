"""The settings window: recording, screenshots, text, clipboard, shortcuts.

Every change is saved at once to settings.json. The GNOME Shell extension
reads the clipboard settings from the same file, and the shortcuts are
GNOME custom shortcuts (see shortcuts.py).
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import shortcuts  # noqa: E402
from .state import settings  # noqa: E402

LIMITS = [(20, "20"), (50, "50"), (100, "100"), (250, "250"), (500, "500"), (1000, "1,000"), (0, "No limit")]
RETENTION = [(0, "Forever"), (1, "1 day"), (7, "1 week"), (30, "30 days"), (90, "90 days")]
COUNTDOWNS = [(0, "Off"), (3, "3 seconds"), (5, "5 seconds"), (10, "10 seconds")]
QUALITIES = [("max", "Maximum", "Sharpest, largest files"), ("high", "High", "Sharp text, smaller files"),
             ("balanced", "Balanced", "Good for long recordings")]
FPS = [(60, "60 fps", "Smooth motion"), (30, "30 fps", "Smaller files")]

# Tesseract pack name -> language name, for the packs people usually install.
LANGUAGE_NAMES = {
    "eng": "English", "fra": "French", "deu": "German", "spa": "Spanish", "ita": "Italian",
    "por": "Portuguese", "nld": "Dutch", "pol": "Polish", "rus": "Russian", "ukr": "Ukrainian",
    "tur": "Turkish", "swe": "Swedish", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish",
    "ces": "Czech", "ell": "Greek", "ara": "Arabic", "chi_sim": "Chinese (simplified)",
    "chi_tra": "Chinese (traditional)", "jpn": "Japanese", "kor": "Korean", "hin": "Hindi",
    "vie": "Vietnamese", "ind": "Indonesian", "heb": "Hebrew", "ron": "Romanian", "hun": "Hungarian",
}


def _combo(title, subtitle, options, key, on_change=None):
    """A ComboRow over (value, label[, description]) options bound to a setting."""
    row = Adw.ComboRow(title=title, subtitle=subtitle or "")
    row.set_model(Gtk.StringList.new([o[1] for o in options]))
    values = [o[0] for o in options]
    current = settings.get(key)
    row.set_selected(values.index(current) if current in values else 0)

    def changed(r, _p):
        settings.set(key, values[r.get_selected()])
        if on_change:
            on_change()
    row.connect("notify::selected", changed)
    return row


def _switch(title, subtitle, key, on_change=None):
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "")
    row.set_active(bool(settings.get(key)))

    def changed(r, _p):
        settings.set(key, r.get_active())
        if on_change:
            on_change()
    row.connect("notify::active", changed)
    return row


def _home_relative(path):
    p = Path(path).expanduser()
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Framewise Settings", search_enabled=True,
                         default_width=640, default_height=720)
        self.app = app
        self.add(self._recording_page())
        self.add(self._screenshots_page())
        self.add(self._clipboard_page())
        self.add(self._shortcuts_page())
        self.add(self._about_page())

    # ---- pages ---------------------------------------------------------------------------

    def _recording_page(self):
        page = Adw.PreferencesPage(title="Recording", icon_name="media-record-symbolic")
        files = Adw.PreferencesGroup(title="Output")
        files.add(self._folder_row("Recordings folder", "folder"))
        files.add(_switch("Open the editor after recording",
                          "Off: the recording is saved as an MP4 straight away", "open_editor"))
        page.add(files)

        video = Adw.PreferencesGroup(title="Video",
                                     description="Defaults for new recordings. The record window can change them for each recording.")
        video.add(_combo("Quality", None, QUALITIES, "quality"))
        video.add(_combo("Frame rate", None, FPS, "fps"))
        video.add(_combo("Countdown", "Before the recording starts", COUNTDOWNS, "countdown"))
        page.add(video)

        sound = Adw.PreferencesGroup(title="Audio")
        sound.add(_switch("System audio", "Recorded on its own track", "system_audio"))
        sound.add(_switch("Microphone", "Recorded on its own track", "microphone"))
        page.add(sound)

        privacy = Adw.PreferencesGroup(title="Privacy")
        forget = Adw.ActionRow(title="Reset screen permissions",
                               subtitle="Ubuntu will ask again before recording each display")
        button = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)

        def do_forget(b):
            from . import portal
            portal.forget_screen()
            b.set_label("Done")
            b.set_sensitive(False)
        button.connect("clicked", do_forget)
        forget.add_suffix(button)
        privacy.add(forget)
        page.add(privacy)
        page.add(self._speech_group())
        return page

    def _screenshots_page(self):
        page = Adw.PreferencesPage(title="Screenshots", icon_name="applets-screenshooter-symbolic")
        group = Adw.PreferencesGroup(title="Screenshots")
        group.add(self._folder_row("Screenshots folder", "shot_folder"))
        group.add(_switch("Copy to clipboard", "Copy each screenshot when it's taken",
                          "shot_auto_copy"))
        group.add(_switch("Include cursor", None, "shot_pointer"))
        group.add(_switch("Show magnifier", "Press Z to toggle it while selecting",
                          "shot_loupe"))
        page.add(group)
        page.add(self._text_group())
        return page

    def _clipboard_page(self):
        page = Adw.PreferencesPage(title="Clipboard", icon_name="edit-paste-symbolic")
        main = Adw.PreferencesGroup(
            title="Clipboard history",
            description="While Framewise is running, copied items are saved on this computer only. "
                        "Items marked as secret by password managers are never saved.")
        self.clip_on = _switch("Enable clipboard history", None, "clipboard_history",
                               on_change=self._clip_sensitive)
        main.add(self.clip_on)
        page.add(main)

        self.clip_options = Adw.PreferencesGroup()
        self.clip_options.add(_switch("Include images and files", "When off, only text is saved", "clipboard_images"))
        self.clip_options.add(_switch("Ignore sensitive text",
                                      "Skips text that looks like a password, key or API token",
                                      "clipboard_skip_sensitive"))
        self.clip_options.add(_combo("History limit", "Pinned items are not counted", LIMITS, "clipboard_limit"))
        self.clip_options.add(_combo("Retention period", "Older items are removed even below the limit. "
                                     "Pinned items are always kept.", RETENTION, "clipboard_retention_days"))
        page.add(self.clip_options)
        self._clip_sensitive()

        shell = self.app.session.shell
        if not shell.has_clipboard:
            note = Adw.PreferencesGroup()
            note.add(Adw.ActionRow(title="Waiting for the Framewise GNOME extension",
                                   subtitle="Clipboard history starts after you log out and back in.",
                                   css_classes=["property"]))
            page.add(note)
        return page

    def _clip_sensitive(self):
        self.clip_options.set_sensitive(bool(settings.get("clipboard_history")))

    def _shortcuts_page(self):
        page = Adw.PreferencesPage(title="Shortcuts", icon_name="input-keyboard-symbolic")
        group = Adw.PreferencesGroup(
            title="Keyboard shortcuts",
            description="Global shortcuts work even when Framewise is not open. "
                        "Super is the Windows key.")
        if not shortcuts.available():
            group.add(Adw.ActionRow(title="Shortcuts can't be set on this desktop",
                                    subtitle="They need GNOME's keyboard settings."))
        else:
            for sid, _name, label, _cmd, suggested in shortcuts.SHORTCUTS:
                group.add(self._shortcut_row(sid, label, suggested))
        page.add(group)
        if shortcuts.available():
            keys = Adw.PreferencesGroup()
            row = Adw.SwitchRow(title="Use Print Screen key",
                                subtitle="Replaces Ubuntu's screenshot tool on the Print key. "
                                         "Shift+Print and Alt+Print are unchanged.")
            row.set_active(shortcuts.print_key_enabled())
            row.connect("notify::active", lambda r, _p: shortcuts.set_print_key(r.get_active()))
            keys.add(row)
            page.add(keys)
        return page

    def _about_page(self):
        from .home import HANDLE, HANDLE_URL
        page = Adw.PreferencesPage(title="About", icon_name="help-about-symbolic")

        top = Adw.PreferencesGroup()
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_bottom=6)
        hero.append(Gtk.Image(icon_name="com.gitcotech.Framewise", pixel_size=96))
        hero.append(Gtk.Label(label="Framewise", css_classes=["title-1"]))
        from .home import credit_label
        hero.append(credit_label())
        top.add(hero)
        page.add(top)

        ready = Adw.PreferencesGroup(title="Startup")
        ready.add(_switch("Show in top bar", "The Framewise menu in the top bar, while Framewise is running",
                          "topbar_icon"))
        login = Adw.SwitchRow(title="Launch at login",
                              subtitle="Start Framewise automatically when you log in")
        login.set_active(autostart_enabled())
        login.connect("notify::active", lambda r, _p: set_autostart(r.get_active()))
        ready.add(login)
        page.add(ready)

        from .home import SUPPORT_URL
        support = Adw.PreferencesGroup(
            title="Support Framewise",
            description="Framewise is free and open source, and it will stay that way. If it saves you "
                        "time, a coffee helps keep it going. Thank you!")
        coffee = Adw.ActionRow(title="Buy Me a Coffee", subtitle="buymeacoffee.com/framewise")
        coffee.add_prefix(Gtk.Label(label="☕"))
        open_btn = Gtk.Button(icon_name="adw-external-link-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Open in browser", css_classes=["flat"])
        open_btn.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(SUPPORT_URL, None))
        coffee.add_suffix(open_btn)
        coffee.set_activatable_widget(open_btn)
        support.add(coffee)
        page.add(support)

        group = Adw.PreferencesGroup(title="Information")
        from . import __version__
        group.add(Adw.ActionRow(title="Version", subtitle=__version__, css_classes=["property"]))
        maker = Adw.ActionRow(title="Developer", subtitle=HANDLE, css_classes=["property"])
        channel = Gtk.Button(label="Telegram", valign=Gtk.Align.CENTER)
        channel.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(HANDLE_URL, None))
        maker.add_suffix(channel)
        group.add(maker)
        version = self.app.session.shell.version()
        if version:
            status = f"Version {'.'.join(map(str, version))} is running"
        else:
            status = "Not running. Log out and back in after installing; it is needed for the " \
                     "top-bar menu, screenshots without a dialog and clipboard history."
        group.add(Adw.ActionRow(title="GNOME extension", subtitle=status, css_classes=["property"]))
        group.add(Adw.ActionRow(title="License", subtitle="GPL-3.0-or-later",
                                css_classes=["property"]))
        from . import gpu
        group.add(Adw.ActionRow(title="Video acceleration", subtitle=gpu.summary(), css_classes=["property"]))
        from .state import CONFIG_DIR, DATA_DIR
        for title, path in (("Configuration", CONFIG_DIR / "settings.json"), ("Data folder", DATA_DIR)):
            row = Adw.ActionRow(title=title, subtitle=_home_relative(path), css_classes=["property"])
            open_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text="Show in Files", css_classes=["flat"])
            folder = path if path.is_dir() else path.parent
            open_btn.connect("clicked", lambda _b, f=folder: Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(str(f)), None))
            row.add_suffix(open_btn)
            group.add(row)
        page.add(group)
        return page

    # ---- pieces ----------------------------------------------------------------------------

    def _folder_row(self, title, key):
        row = Adw.ActionRow(title=title, subtitle=_home_relative(settings.get(key)))
        button = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)

        def pick(_b):
            dialog = Gtk.FileDialog(title=title)
            current = Path(settings.get(key)).expanduser()
            if current.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(current)))

            def chosen(d, result):
                try:
                    folder = d.select_folder_finish(result)
                except GLib.Error:
                    return
                if folder and folder.get_path():
                    settings.set(key, folder.get_path())
                    row.set_subtitle(_home_relative(folder.get_path()))
            dialog.select_folder(self, None, chosen)
        button.connect("clicked", pick)
        row.add_suffix(button)
        row.set_activatable_widget(button)
        return row

    def _text_group(self):
        from . import ocr
        group = Adw.PreferencesGroup(title="Text capture (OCR)")
        if not ocr.available():
            group.add(Adw.ActionRow(title="Text recognition isn't installed",
                                    subtitle="sudo apt install tesseract-ocr", css_classes=["property"]))
            return group
        langs = ocr.pick_languages(ocr.installed_languages())
        names = [LANGUAGE_NAMES.get(code, code) for code in langs]
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        # One language: just its name; several: they're told apart automatically.
        found = listed if len(names) == 1 else f"{listed}, detected automatically"
        example = "fra" if "fra" not in langs else "spa"
        group.add(Adw.ActionRow(
            title="Languages", subtitle=f"{found}. Add a language with its language pack, "
                                        f"for example: sudo apt install tesseract-ocr-{example}",
            css_classes=["property"]))
        return group

    def _speech_group(self):
        from . import speech
        group = Adw.PreferencesGroup(
            title="Speech recognition",
            description="Used by the editor to detect filler words (um, uh). Runs locally; "
                        "recordings are never uploaded.")
        self.speech_row = Adw.ActionRow(css_classes=["property"])
        self.speech_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.speech_button.connect("clicked", lambda *_: self._speech_clicked())
        self.speech_progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, visible=False, width_request=120)
        self.speech_row.add_suffix(self.speech_progress)
        self.speech_row.add_suffix(self.speech_button)
        group.add(self.speech_row)
        self._speech_busy = False
        self._update_speech()
        return group

    def _update_speech(self):
        from . import speech
        self.speech_row.set_title("Speech model")
        if speech.is_ready():
            size = _folder_size(speech.SPEECH_DIR)
            self.speech_row.set_subtitle(f"Installed ({GLib.format_size(size)})")
            self.speech_button.set_label("Remove")
            self.speech_button.set_css_classes(["destructive-action"])
        else:
            self.speech_row.set_subtitle(f"Not downloaded ({speech.DOWNLOAD_SIZE})")
            self.speech_button.set_label("Download")
            self.speech_button.set_css_classes(["suggested-action"])
        self.speech_button.set_sensitive(not self._speech_busy)

    def _speech_clicked(self):
        from . import speech
        if speech.is_ready():
            dialog = Adw.AlertDialog(heading="Remove speech recognition?",
                                     body="Finding filler words will offer to download it again.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("remove", "Remove")
            dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

            def answered(_d, response):
                if response == "remove":
                    speech.remove()
                    self._update_speech()
            dialog.connect("response", answered)
            dialog.present(self)
            return
        if not speech.can_install():
            self.speech_row.set_subtitle("Needs one Ubuntu package first: sudo apt install python3-venv")
            return
        self._speech_busy = True
        self.speech_progress.set_visible(True)
        self._update_speech()

        def progress(label, fraction):
            self.speech_row.set_subtitle(f"{label}…")
            self.speech_progress.set_fraction(fraction)

        def done(ok, message):
            self._speech_busy = False
            self.speech_progress.set_visible(False)
            self._update_speech()
            if not ok:
                self.speech_row.set_subtitle(f"The download didn't finish: {message[-160:]}")
        speech.install_async(progress, done)

    def _shortcut_row(self, sid, label, suggested):
        row = Adw.ActionRow(title=label)
        keys = Gtk.ShortcutLabel(valign=Gtk.Align.CENTER, disabled_text="Off")
        keys.set_accelerator(shortcuts.binding(sid))
        change = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Change", css_classes=["flat"])
        change.connect("clicked", lambda *_: self._ask_keys(sid, label, keys))
        row.add_suffix(keys)
        row.add_suffix(change)
        row.set_activatable_widget(change)
        return row

    def _ask_keys(self, sid, label, keys_label):
        dialog = Adw.AlertDialog(heading=label,
                                 body="Press the new keys, for example Shift+Super+S.\n"
                                      "Backspace turns the shortcut off. Esc cancels.")
        dialog.add_response("cancel", "Cancel")
        preview = Gtk.ShortcutLabel(halign=Gtk.Align.CENTER, disabled_text="…",
                                    accelerator=shortcuts.binding(sid))
        warning = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(preview)
        box.append(warning)
        dialog.set_extra_child(box)
        controller = Gtk.EventControllerKey()

        def pressed(_c, keyval, _code, state):
            mods = state & Gtk.accelerator_get_default_mod_mask()
            lower = Gdk.keyval_to_lower(keyval)
            if lower in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
                         Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L,
                         Gdk.KEY_Meta_R, Gdk.KEY_ISO_Level3_Shift):
                return False
            if not mods and lower == Gdk.KEY_Escape:
                dialog.close()
                return True
            if not mods and lower == Gdk.KEY_BackSpace:
                shortcuts.set_binding(sid, "")
                keys_label.set_accelerator("")
                dialog.close()
                return True
            function_key = Gdk.KEY_F1 <= lower <= Gdk.KEY_F35 or lower == Gdk.KEY_Print
            if not (mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                            | Gdk.ModifierType.SUPER_MASK)) and not function_key:
                warning.set_label("Use Ctrl, Alt or Super with the key, so it doesn't stop you typing.")
                return True
            accel = Gtk.accelerator_name(lower, mods)
            preview.set_accelerator(accel)
            other = shortcuts.used_by(accel, except_sid=sid)
            if other:
                warning.set_label(f"Already used for “{other}”. Try another combination.")
                return True
            shortcuts.set_binding(sid, accel)
            keys_label.set_accelerator(accel)
            GLib.timeout_add(350, lambda: (dialog.close(), False)[1])
            return True
        controller.connect("key-pressed", pressed)
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        dialog.add_controller(controller)
        dialog.present(self)


AUTOSTART = Path(GLib.get_user_config_dir()) / "autostart" / "com.gitcotech.Framewise.desktop"


def autostart_enabled():
    return AUTOSTART.exists()


def set_autostart(on):
    if not on:
        AUTOSTART.unlink(missing_ok=True)
        return
    from .shortcuts import _launcher
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(
        "[Desktop Entry]\nType=Application\nName=Framewise\n"
        "Comment=Keeps Framewise ready in the background\n"
        f"Exec={_launcher()} --background\nIcon=com.gitcotech.Framewise\n"
        "NoDisplay=true\nX-GNOME-Autostart-enabled=true\n")


def _folder_size(path):
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total
