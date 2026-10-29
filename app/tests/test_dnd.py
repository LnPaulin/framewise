import unittest
from unittest import mock

from gi.repository import Gio

from framewise import dnd


class FakeStore:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


def memory_settings():
    """GNOME's notification settings, in memory only (never the real ones)."""
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(dnd.SCHEMA, True) is None:
        return None
    return Gio.Settings.new_with_backend(dnd.SCHEMA, Gio.memory_settings_backend_new())


@unittest.skipIf(memory_settings() is None, "GNOME notification settings not installed")
class DndTests(unittest.TestCase):
    def setUp(self):
        self.gs = memory_settings()
        self.store = FakeStore()
        self.d = dnd.DoNotDisturb(self.gs, self.store)
        patcher = mock.patch.object(dnd.settings, "get", side_effect=lambda k: self.enabled if k == "recording_dnd" else None)
        self.enabled = True
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_on_while_recording_then_back(self):
        self.assertTrue(self.gs.get_boolean(dnd.KEY))
        self.d.begin()
        self.assertFalse(self.gs.get_boolean(dnd.KEY))
        self.assertTrue(self.d.active)
        self.d.end()
        self.assertTrue(self.gs.get_boolean(dnd.KEY))
        self.assertFalse(self.d.active)

    def test_already_on_is_left_on(self):
        self.gs.set_boolean(dnd.KEY, False)        # the person has DND on
        self.d.begin()
        self.d.end()
        self.assertFalse(self.gs.get_boolean(dnd.KEY))

    def test_setting_off_does_nothing(self):
        self.enabled = False
        self.d.begin()
        self.assertTrue(self.gs.get_boolean(dnd.KEY))
        self.assertFalse(self.d.active)

    def test_end_twice_is_harmless(self):
        self.d.begin()
        self.d.end()
        self.gs.set_boolean(dnd.KEY, False)        # turned on by hand afterwards
        self.d.end()
        self.assertFalse(self.gs.get_boolean(dnd.KEY))

    def test_restored_after_a_crash(self):
        self.d.begin()
        # Framewise dies; a new one starts with the same note.
        again = dnd.DoNotDisturb(self.gs, self.store)
        again.end()
        self.assertTrue(self.gs.get_boolean(dnd.KEY))


if __name__ == "__main__":
    unittest.main()
