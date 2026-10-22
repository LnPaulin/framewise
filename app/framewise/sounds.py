"""Short sounds from the desktop's sound theme (e.g. the camera shutter).

Quiet when "event sounds" are off in GNOME's settings, or the sound theme
isn't installed.
"""

from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, Gst  # noqa: E402

THEME = Path("/usr/share/sounds/freedesktop/stereo")
_playing = set()   # players kept alive until they finish


def _enabled():
    try:
        source = Gio.SettingsSchemaSource.get_default()
        if source and source.lookup("org.gnome.desktop.sound", True):
            return Gio.Settings.new("org.gnome.desktop.sound").get_boolean("event-sounds")
    except Exception:  # noqa: BLE001 - a missing schema just means "play"
        pass
    return True


def play(name):
    """name: a sound in the freedesktop theme, e.g. "camera-shutter"."""
    path = THEME / f"{name}.oga"
    if not path.exists() or not _enabled():
        return
    Gst.init(None)
    player = Gst.ElementFactory.make("playbin", None)
    if player is None:
        return
    player.set_property("uri", path.as_uri())
    player.set_property("video-sink", Gst.ElementFactory.make("fakesink", None))
    bus = player.get_bus()
    bus.add_signal_watch()

    def finished(*_):
        bus.remove_signal_watch()
        player.set_state(Gst.State.NULL)
        _playing.discard(player)
    bus.connect("message::eos", finished)
    bus.connect("message::error", finished)
    _playing.add(player)
    player.set_state(Gst.State.PLAYING)
