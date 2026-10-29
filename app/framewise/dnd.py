"""Do Not Disturb while recording.

Notification pop-ups (banners) would land in the video, and they can show
private messages. While a recording runs, GNOME's Do Not Disturb is turned
on; afterwards it's turned off again, but only if Framewise turned it on
(someone who had it on already keeps it on).

That Framewise turned it on is remembered in state.json, so if Framewise
crashes or is closed mid-recording, the next start turns pop-ups back on.
Notifications still arrive meanwhile; they wait in the notification list.
"""

from gi.repository import Gio

from . import state
from .state import settings

SCHEMA = "org.gnome.desktop.notifications"
KEY = "show-banners"
REMEMBER = "dnd_restore"       # in state.json: True while Framewise has DND on


def _gnome_settings():
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(SCHEMA, True) is None:
        return None
    return Gio.Settings.new(SCHEMA)


class DoNotDisturb:
    def __init__(self, gsettings=None, store=None):
        """gsettings: GNOME's notification settings (tests pass their own);
        store: where the "turned on by Framewise" note is kept."""
        self._gs = gsettings
        self._store = store or state

    def _settings(self):
        if self._gs is None:
            self._gs = _gnome_settings()
        return self._gs

    def begin(self):
        """Turns DND on for a recording, when the setting is on."""
        if not settings.get("recording_dnd"):
            return
        gs = self._settings()
        if gs is None or not gs.get_boolean(KEY):
            return                      # no such setting, or DND is on already
        self._store.set(REMEMBER, True)  # noted first: a crash right after still restores
        gs.set_boolean(KEY, False)
        Gio.Settings.sync()

    def end(self):
        """Turns pop-ups back on if Framewise turned them off. Safe to call
        any number of times, and at start-up (after a crash)."""
        if not self._store.get(REMEMBER):
            return
        gs = self._settings()
        if gs is not None:
            gs.set_boolean(KEY, True)
            Gio.Settings.sync()
        self._store.set(REMEMBER, None)

    @property
    def active(self):
        return bool(self._store.get(REMEMBER))
