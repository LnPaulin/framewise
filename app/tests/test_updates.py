"""Update helpers: version numbers, release links, extension version, announcing once."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from framewise import updates


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updates.parse("1.0.10"), (1, 0, 10))
        self.assertEqual(updates.parse("v1.2"), (1, 2))
        self.assertGreater(updates.parse("1.0.10"), updates.parse("1.0.9"))
        for bad in ("", "latest", "1.0.0-beta", "../x", "1.0; rm"):
            self.assertEqual(updates.parse(bad), ())

    def test_release_page_only_for_real_versions(self):
        self.assertTrue(updates.release_page("1.0.2").endswith("/releases/tag/v1.0.2"))
        self.assertEqual(updates.release_page("javascript:alert(1)"), updates.DOWNLOAD_PAGE)


class ExtensionVersionTest(unittest.TestCase):
    def test_home_copy_wins(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as system:
            for base, version in ((home, "1.9"), (system, "1.8")):
                d = Path(base) / "gnome-shell" / "extensions" / updates.EXTENSION
                d.mkdir(parents=True)
                (d / "metadata.json").write_text(json.dumps({"version-name": version}))
            with mock.patch.object(updates.GLib, "get_user_data_dir", return_value=home), \
                    mock.patch.object(updates.GLib, "get_system_data_dirs", return_value=[system]):
                self.assertEqual(updates.installed_extension_version(), (1, 9))


class NotificationTest(unittest.TestCase):
    """The real notifications are built without errors."""

    def setUp(self):
        self.saved = {"last_run_version": "0.9"}
        patcher = mock.patch.object(updates, "settings", mock.Mock(
            get=lambda k: self.saved.get(k), set=lambda k, v: self.saved.__setitem__(k, v)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = mock.Mock()
        self.app.session.shell.version.return_value = ()
        self.u = updates.Updates(self.app)

    def test_updated_notice(self):
        self.u._note_update()
        self.app.send_notification.assert_called_once()
        self.assertEqual(self.saved["last_run_version"], updates.__version__)

    def test_available_notice(self):
        self.u._announce("99.0.0")
        self.app.send_notification.assert_called_once()


class AnnounceTest(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        patcher = mock.patch.object(updates, "settings", mock.Mock(
            get=lambda k: self.saved.get(k), set=lambda k, v: self.saved.__setitem__(k, v)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.u = updates.Updates(mock.Mock())
        self.u._announce = mock.Mock()

    def test_newer_version_announced_once(self):
        newer = "99.0.0"
        self.u._checked(newer, None, None)
        self.u._checked(newer, None, None)
        self.u._announce.assert_called_once_with(newer)

    def test_same_or_older_not_announced(self):
        self.u._checked(updates.__version__, None, None)
        self.u._checked("0.1", None, None)
        self.u._announce.assert_not_called()


if __name__ == "__main__":
    unittest.main()
