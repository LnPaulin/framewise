"""The window the Framewise icon opens: every tool, with its shortcut."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from . import shortcuts  # noqa: E402

CREDIT = "Brought to you by"
HANDLE = "@gitcotech"
HANDLE_URL = "https://t.me/gitcotech"
SUPPORT_URL = "https://buymeacoffee.com/framewise"

# (app action, title, description (for the code only, not shown), icon, shortcut id)
TOOLS = [
    ("record", "Screen Recording", "An area, a window or the whole screen", "media-record-symbolic", "framewise-record"),
    ("screenshot", "Screenshot", "Capture, mark up and pin", "applets-screenshooter-symbolic",
     "framewise-screenshot"),
    ("text", "Text Capture", "Read the text in any part of the screen", "edit-copy-symbolic", "framewise-text"),
    ("clipboard", "Clipboard History", "Paste something you copied earlier", "edit-paste-symbolic",
     "framewise-clipboard"),
    ("camera", "Camera", "Your webcam in a floating bubble, or as a mirror", "camera-web-symbolic",
     "framewise-camera"),
]

CSS = """
.fw-home-tool { padding: 14px 16px; border-radius: 14px; }
.fw-home-tool image.fw-home-icon { -gtk-icon-size: 28px; color: @accent_color; }
.fw-home-title { font-weight: bold; font-size: 1.05em; }
.fw-home-keys { font-size: 0.85em; }
.fw-home-credit { font-size: 0.9em; }
"""


def credit_label():
    """'Brought to you by @gitcotech', with the handle linking to the channel."""
    label = Gtk.Label(use_markup=True, css_classes=["dim-label", "fw-home-credit"])
    label.set_markup(f'{CREDIT} <a href="{HANDLE_URL}">{HANDLE}</a>')
    return label


class Home(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Framewise", resizable=False, default_width=440)
        self.app = app
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Adw.HeaderBar()
        header.pack_end(Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings",
                                   action_name="app.preferences"))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       margin_top=6, margin_bottom=18, margin_start=18, margin_end=18)
        logo = Gtk.Image(icon_name="com.gitcotech.Framewise", pixel_size=72, margin_bottom=2)
        body.append(logo)
        body.append(Gtk.Label(label="Framewise", css_classes=["title-1"]))
        # The credit and the support link are in Settings → About.
        if not app.session.shell.version():
            # Installed but the GNOME extension isn't running yet.
            note = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, margin_top=10, max_width_chars=44,
                             label="To finish setup, log out and back in. This enables the top bar menu, "
                                   "clipboard history and recording controls.",
                             css_classes=["dim-label", "fw-home-note"])
            body.append(note)

        tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=14)
        for action, title, description, icon, sid in TOOLS:
            tools.append(self._tool(action, title, description, icon, sid))
        body.append(tools)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_content(view)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, k, *_: k == Gdk.KEY_Escape and (self.close() or True))
        self.add_controller(keys)

    def _tool(self, action, title, description, icon, sid):
        row = Gtk.Box(spacing=14)
        row.append(Gtk.Image(icon_name=icon, css_classes=["fw-home-icon"]))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(Gtk.Label(label=title, xalign=0, css_classes=["fw-home-title"]))
        row.append(text)
        accel = shortcuts.binding(sid)
        if accel:
            row.append(Gtk.ShortcutLabel(accelerator=accel, valign=Gtk.Align.CENTER,
                                         css_classes=["fw-home-keys"]))
        button = Gtk.Button(child=row, css_classes=["fw-home-tool", "card"])

        def clicked(_b):
            if action == "clipboard":
                # Opens over this window; nothing to hide.
                self.app.activate_action(action, None)
                return
            # Hide first and give the screen a moment: it is captured right after.
            # This window comes back once the tool is done.
            self.app.hold()
            self.app.back_home_later()
            self.set_visible(False)

            def run():
                self.app.activate_action(action, None)
                self.close()
                self.app.release()
                return False
            GLib.timeout_add(200, run)
        button.connect("clicked", clicked)
        return button
