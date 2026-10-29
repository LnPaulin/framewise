"""One-time setup for each person who runs Framewise.

A package installs Framewise for the whole computer, but keyboard shortcuts
and GNOME extensions are switched on per person. This runs when Framewise
starts and does that the first time (and again if the program moved, so the
shortcuts and "Launch at login" point at it).
"""

from gi.repository import Gio, GLib

from . import shortcuts
from . import state

EXTENSION = "framewise@gitcotech"
SETUP_VERSION = 1
SHORTCUTS_VERSION = 6   # 2: color picker, 3: dictation, 4: draw, 5: scrolling screenshot (no keys), 6: sound


def ensure():
    """Returns True when something was set up just now."""
    launcher = shortcuts._launcher()
    done = state.get("user_setup") or {}
    if done.get("version") == SETUP_VERSION and done.get("launcher") == launcher:
        _new_shortcuts()
        return False
    try:
        shortcuts.setup()
        shortcuts.refresh_print_key()
        _enable_extension()
        _refresh_autostart()
    except GLib.Error:
        return False
    state.set("user_setup", {"version": SETUP_VERSION, "launcher": launcher})
    state.set("shortcuts_version", SHORTCUTS_VERSION)
    return True


def _new_shortcuts():
    """Shortcuts added in an update get their keys once (unless the keys are
    taken); the ones already set up keep theirs, even when turned off."""
    if (state.get("shortcuts_version") or 1) >= SHORTCUTS_VERSION:
        return
    try:
        shortcuts.setup()
    except GLib.Error:
        return
    state.set("shortcuts_version", SHORTCUTS_VERSION)


def _refresh_autostart():
    """"Launch at login" points at the program: keep it pointing at this one."""
    from .settings_window import autostart_enabled, set_autostart
    if autostart_enabled():
        set_autostart(True)


def _enable_extension():
    """Adds the extension to GNOME's list; it starts at the next login."""
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup("org.gnome.shell", True) is None:
        return
    shell = Gio.Settings.new("org.gnome.shell")
    enabled = shell.get_strv("enabled-extensions")
    if EXTENSION not in enabled:
        shell.set_strv("enabled-extensions", enabled + [EXTENSION])
    disabled = shell.get_strv("disabled-extensions")
    if EXTENSION in disabled:
        shell.set_strv("disabled-extensions", [e for e in disabled if e != EXTENSION])
    Gio.Settings.sync()
