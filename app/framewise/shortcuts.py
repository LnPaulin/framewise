"""framewise's keyboard shortcuts, kept as GNOME custom shortcuts.

GNOME runs the command when the keys are pressed, so the shortcuts work
even when framewise isn't running. Each one lives at a fixed path under
the media-keys custom keybindings.
"""

import shutil
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio  # noqa: E402

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ITEM_SCHEMA = SCHEMA + ".custom-keybinding"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def _launcher():
    found = shutil.which("framewise")
    return found or str(Path.home() / ".local" / "bin" / "framewise")


# (id, name shown in GNOME Settings, label here, command, suggested keys)
SHORTCUTS = [
    ("framewise-record", "Screen recording (Framewise)", "Record screen",
     lambda: f"{_launcher()} record", "<Shift><Super>r"),
    ("framewise-screenshot", "Screenshot (Framewise)", "Take screenshot",
     lambda: f"{_launcher()} screenshot", "<Shift><Super>s"),
    ("framewise-text", "Text capture (Framewise)", "Capture text",
     lambda: f"{_launcher()} text", "<Shift><Super>t"),
    ("framewise-clipboard", "Clipboard history (Framewise)", "Clipboard history",
     lambda: f"{_launcher()} clipboard", "<Shift><Super>v"),
    ("framewise-camera", "Camera (Framewise)", "Toggle camera",
     lambda: f"{_launcher()} camera", "<Shift><Super>c"),
]

# GNOME's own shortcuts, to warn before taking keys it already uses.
GNOME_SCHEMAS = ["org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings",
                 "org.gnome.mutter.keybindings", "org.gnome.mutter.wayland.keybindings",
                 "org.gnome.settings-daemon.plugins.media-keys"]


def available():
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(ITEM_SCHEMA, True) is not None


def _item(sid):
    return Gio.Settings.new_with_path(ITEM_SCHEMA, f"{BASE}{sid}/")


def binding(sid):
    """The keys for one shortcut ('' when it is off or not set up)."""
    if not available():
        return ""
    if f"{BASE}{sid}/" not in Gio.Settings.new(SCHEMA).get_strv("custom-keybindings"):
        return ""
    return _item(sid).get_string("binding")


def set_binding(sid, accel):
    """Sets (or with accel '' turns off) one shortcut, creating it if needed."""
    _sid, name, _label, command, _suggested = next(s for s in SHORTCUTS if s[0] == sid)
    media = Gio.Settings.new(SCHEMA)
    paths = media.get_strv("custom-keybindings")
    path = f"{BASE}{sid}/"
    item = _item(sid)
    if accel:
        item.set_string("name", name)
        item.set_string("command", command())
        item.set_string("binding", accel)
        if path not in paths:
            media.set_strv("custom-keybindings", paths + [path])
    else:
        item.set_string("binding", "")
    Gio.Settings.sync()


def same_keys(a, b):
    from gi.repository import Gtk
    ok_a, key_a, mods_a = Gtk.accelerator_parse(a)
    ok_b, key_b, mods_b = Gtk.accelerator_parse(b)
    return ok_a and ok_b and key_a == key_b and mods_a == mods_b


def used_by(accel, except_sid=None):
    """Who else uses these keys: a GNOME action or another custom shortcut, or None."""
    source = Gio.SettingsSchemaSource.get_default()
    for schema_id in GNOME_SCHEMAS:
        schema = source.lookup(schema_id, True) if source else None
        if schema is None:
            continue
        settings = Gio.Settings.new(schema_id)
        for key in schema.list_keys():
            if schema.get_key(key).get_value_type().dup_string() != "as":
                continue
            if any(same_keys(accel, a) for a in settings.get_strv(key) if a):
                return key.replace("-", " ")
    if available():
        for path in Gio.Settings.new(SCHEMA).get_strv("custom-keybindings"):
            if path == f"{BASE}{except_sid}/":
                continue
            item = Gio.Settings.new_with_path(ITEM_SCHEMA, path)
            if item.get_string("binding") and same_keys(accel, item.get_string("binding")):
                return item.get_string("name") or "another shortcut"
    return None


def setup(old_prefix="capturekit-"):
    """Run by the installer: keeps the keys of shortcuts made under the old
    name, and gives each shortcut its suggested keys the first time (unless
    something else already uses them). A shortcut turned off stays off.
    Returns a list of lines to print."""
    if not available():
        return ["GNOME keyboard settings not found: no shortcuts set."]
    media = Gio.Settings.new(SCHEMA)
    paths = media.get_strv("custom-keybindings")
    report = []
    for sid, name, _label, _command, suggested in SHORTCUTS:
        old_id = old_prefix + sid.split("-", 1)[1]
        old_path = f"{BASE}{old_id}/"
        accel = None
        if old_path in paths:
            accel = _item(old_id).get_string("binding")
            old = _item(old_id)
            for key in ("name", "command", "binding"):
                old.reset(key)
            paths = [p for p in paths if p != old_path]
            media.set_strv("custom-keybindings", paths)
        path = f"{BASE}{sid}/"
        if accel is None and path in paths:
            accel = _item(sid).get_string("binding")    # already set up: keep it, even if off
        if accel is None:
            accel = "" if used_by(suggested, except_sid=sid) else suggested
        set_binding(sid, accel) if accel else _ensure_off(sid)
        paths = media.get_strv("custom-keybindings")
        report.append(f"Shortcut: {name}: {accel or 'off'}")
    return report


def _ensure_off(sid):
    """Creates the shortcut with no keys, so Settings can show and change it."""
    _sid, name, _label, command, _suggested = next(s for s in SHORTCUTS if s[0] == sid)
    media = Gio.Settings.new(SCHEMA)
    paths = media.get_strv("custom-keybindings")
    item = _item(sid)
    item.set_string("name", name)
    item.set_string("command", command())
    item.set_string("binding", "")
    if f"{BASE}{sid}/" not in paths:
        media.set_strv("custom-keybindings", paths + [f"{BASE}{sid}/"])
    Gio.Settings.sync()


# ---- the Print Screen key ------------------------------------------------------------

PRINT_ID = "framewise-print"
GNOME_KEYS = "org.gnome.shell.keybindings"
GNOME_PRINT = "show-screenshot-ui"      # GNOME's own screenshot tool


def print_key_enabled():
    return binding(PRINT_ID) == "Print"


def set_print_key(on):
    """Print Screen opens Framewise's screenshot instead of Ubuntu's tool (on),
    or goes back to Ubuntu's tool (off). Only the plain key: Shift+Print and
    Alt+Print stay Ubuntu's."""
    if not available():
        return
    gnome = Gio.Settings.new(GNOME_KEYS)
    keys = gnome.get_strv(GNOME_PRINT)
    media = Gio.Settings.new(SCHEMA)
    paths = media.get_strv("custom-keybindings")
    path = f"{BASE}{PRINT_ID}/"
    item = _item(PRINT_ID)
    if on:
        gnome.set_strv(GNOME_PRINT, [k for k in keys if k != "Print"])
        item.set_string("name", "Screenshot with Print Screen (Framewise)")
        item.set_string("command", f"{_launcher()} screenshot")
        item.set_string("binding", "Print")
        if path not in paths:
            media.set_strv("custom-keybindings", paths + [path])
    else:
        for key in ("name", "command", "binding"):
            item.reset(key)
        media.set_strv("custom-keybindings", [p for p in paths if p != path])
        if "Print" not in keys:
            gnome.set_strv(GNOME_PRINT, keys + ["Print"])
    Gio.Settings.sync()


def refresh_print_key():
    """Keeps the command pointing at the current program (after an install)."""
    if print_key_enabled():
        _item(PRINT_ID).set_string("command", f"{_launcher()} screenshot")
        Gio.Settings.sync()
