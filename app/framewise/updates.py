"""Keeping Framewise up to date.

Updates themselves come through APT: the .deb adds the Framewise repository
(/etc/apt/sources.list.d/framewise.sources), so new versions show up in
Software Updater. This module handles the rest:

- Restart after an update: Framewise keeps running in the background, so
  when its files are replaced it restarts itself, quietly, once nothing is
  open (old code mixed with new files would misbehave).
- After an update: a notification, once, with "What's New", and a reminder
  to log out when the GNOME extension changed too (GNOME loads it at login).
- Update check: about once a day, ask GitHub for the latest release and say
  when a newer one is out (Settings → About can turn this off).
- Update Now: installs the new version through PackageKit (Ubuntu asks for
  the password as usual), then restarts Framewise.
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

from gi.repository import Gio, GLib

from . import __version__
from .state import settings

REPO = "LnPaulin/framewise"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/tag/v{{version}}"
DOWNLOAD_PAGE = f"https://github.com/{REPO}/releases/latest"
APT_SOURCE = Path("/etc/apt/sources.list.d/framewise.sources")
EXTENSION = "framewise@gitcotech"

CHECK_EVERY = 24 * 3600          # seconds between update checks
FIRST_CHECK_DELAY = 120          # seconds after starting
RESTART_SETTLE_MS = 5000         # an upgrade replaces many files: wait for it to finish
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def parse(version):
    """'1.0.10' -> (1, 0, 10); anything else -> ()."""
    version = str(version).strip().lstrip("v")
    return tuple(int(p) for p in version.split(".")) if VERSION_RE.match(version) else ()


def release_page(version):
    return RELEASE_PAGE.format(version=version) if parse(version) else DOWNLOAD_PAGE


def installed_extension_version():
    """The version of the extension GNOME will load at the next login (a copy
    in the home folder wins over the system one), as a tuple."""
    for base in (Path(GLib.get_user_data_dir()), *map(Path, GLib.get_system_data_dirs())):
        meta = base / "gnome-shell" / "extensions" / EXTENSION / "metadata.json"
        try:
            return parse(json.loads(meta.read_text()).get("version-name", ""))
        except (OSError, ValueError):
            continue
    return ()


class Updates:
    def __init__(self, app):
        self.app = app
        self._restart_pending = None
        self._settle_id = 0
        self._monitor = None
        self._checking = False
        self._installing = False

    def start(self):
        self._note_update()
        self._watch_files()
        GLib.timeout_add_seconds(FIRST_CHECK_DELAY, self._tick)
        GLib.timeout_add_seconds(3600, self._tick)

    # ---- after an update -----------------------------------------------------------

    def _note_update(self):
        last = settings.get("last_run_version") or ""
        settings.set("last_run_version", __version__)
        if not last or parse(last) >= parse(__version__):
            return   # first run, or the same version
        body = "See what's new in this version."
        running = self.app.session.shell.version()
        if running and installed_extension_version() > running:
            body = "Log out and back in to finish the update."
        n = Gio.Notification.new(f"Framewise updated to {__version__}")
        n.set_body(body)
        whats_new = Gio.Action.print_detailed_name("app.whats-new", GLib.Variant("s", __version__))
        n.set_default_action(whats_new)
        n.add_button("What's New", whats_new)
        self.app.send_notification("updated", n)

    # ---- restart when the installed files change ---------------------------------------

    def _watch_files(self):
        init = Path(__file__).with_name("__init__.py")
        try:
            self._monitor = Gio.File.new_for_path(str(init)).monitor_file(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return
        self._monitor.connect("changed", self._on_changed)

    def _on_changed(self, *_):
        if self._settle_id:
            GLib.source_remove(self._settle_id)
        self._settle_id = GLib.timeout_add(RESTART_SETTLE_MS, self._check_new_files)

    def _check_new_files(self):
        self._settle_id = 0
        try:
            text = Path(__file__).with_name("__init__.py").read_text()
            new = re.search(r'__version__ = "([^"]+)"', text).group(1)
        except (OSError, AttributeError):
            return False
        if new != __version__ and self._restart_pending is None:
            self._restart_pending = new
            GLib.timeout_add_seconds(2, self._restart_when_idle)
        return False

    def _busy(self):
        app = self.app
        if any(w.get_visible() for w in app.get_windows()):
            return True
        if app.session.phase != app.session.IDLE:
            return True
        shots = app.shots
        return bool(shots is not None and (shots.busy or shots.card))

    def _restart_when_idle(self):
        if self._busy():
            return True   # again in 2 s
        self.restart()
        return False

    def restart(self, command="--background"):
        """Starts the new version in place of this one (same process, so the
        app's D-Bus name moves over cleanly), in the background or with a
        command (e.g. "settings" to come back to Settings)."""
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, "-P", "-m", "framewise", command])

    # ---- Update Now ----------------------------------------------------------------------

    @property
    def can_install(self):
        """Installed from the package with PackageKit around (else: Software Updater)."""
        return APT_SOURCE.exists() and _packagekit() is not None

    @property
    def installing(self):
        return self._installing

    def install(self, on_progress=None, on_done=None):
        """Updates Framewise now. on_progress(percent or None, text) and
        on_done(ok, message) run on the main loop. Restarts Framewise after a
        successful update when nothing is in progress."""
        if self._installing:
            return
        pk = _packagekit()
        if pk is None or not APT_SOURCE.exists():
            open_updater()
            return
        self._installing = True

        stage = ["Looking for the update…"]

        def report(percent, text):
            stage[0] = text
            if on_progress:
                GLib.idle_add(lambda: on_progress(percent, text) and False)

        def progress(prog, kind, *_):
            if kind == pk.ProgressType.PERCENTAGE:
                value = prog.get_property("percentage")
                report(value if 0 <= value <= 100 else None, stage[0])

        def work():
            ok, message = False, ""
            try:
                client = pk.Client()
                report(None, "Looking for the update…")
                client.refresh_cache(False, None, progress, None)
                found = [p for p in client.get_updates(1 << int(pk.FilterEnum.NONE), None, progress, None)
                         .get_package_array() if p.get_name() == "framewise"]
                if not found:
                    ok, message = True, "Framewise is up to date"
                else:
                    report(None, "Installing…")
                    result = client.update_packages(1 << int(pk.TransactionFlagEnum.ONLY_TRUSTED),
                                                    [found[0].get_id()], None, progress, None)
                    ok = result.get_exit_code() == pk.ExitEnum.SUCCESS
                    message = "Updated" if ok else "The update didn't finish"
            except GLib.Error as e:
                message = _friendly_error(e.message)
            GLib.idle_add(self._installed, ok, message, on_done)
        threading.Thread(target=work, daemon=True).start()

    def _installed(self, ok, message, on_done):
        self._installing = False
        if on_done:
            on_done(ok, message)
        if ok and message == "Updated":
            self._restart_after_install()
        elif not ok and on_done is None:
            n = Gio.Notification.new("Framewise couldn't be updated")
            n.set_body(message)
            self.app.send_notification("update-failed", n)
        return False

    def _restart_after_install(self):
        """Right away if only Settings or the home window is open (Settings
        comes back); otherwise once the work in progress is done."""
        app = self.app
        names = {type(w).__name__ for w in app.get_windows() if w.get_visible()}
        if app.session.phase == app.session.IDLE and names <= {"SettingsWindow", "Home"}:
            self.restart("settings" if "SettingsWindow" in names else "--background")
        elif self._restart_pending is None:
            self._restart_pending = "installed"
            GLib.timeout_add_seconds(2, self._restart_when_idle)

    # ---- update check ------------------------------------------------------------------

    def _tick(self):
        if settings.get("check_updates") and time.time() - float(settings.get("last_update_check") or 0) >= CHECK_EVERY:
            self.check()
        return True

    def check(self, on_result=None):
        """Asks GitHub for the latest version, in the background.
        on_result(latest_version_or_None, error_or_None) on the main loop."""
        if self._checking:
            return
        self._checking = True
        settings.set("last_update_check", time.time())

        def work():
            latest, error = None, None
            try:
                req = urllib.request.Request(LATEST_API, headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"Framewise/{__version__}"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    tag = json.loads(resp.read(200_000)).get("tag_name", "")
                latest = tag.lstrip("v") if parse(tag) else None
                if latest is None:
                    error = "No version found"
            except (OSError, ValueError) as e:
                error = str(e)
            GLib.idle_add(self._checked, latest, error, on_result)
        threading.Thread(target=work, daemon=True).start()

    def _checked(self, latest, error, on_result):
        self._checking = False
        if latest and parse(latest) > parse(__version__) and settings.get("update_notified") != latest:
            settings.set("update_notified", latest)
            self._announce(latest)
        if on_result:
            on_result(latest, error)
        return False

    def _announce(self, latest):
        n = Gio.Notification.new(f"Framewise {latest} is available")
        whats_new = Gio.Action.print_detailed_name("app.whats-new", GLib.Variant("s", latest))
        if self.can_install:
            n.set_body("Update now, or later from Software Updater.")
            n.set_default_action("app.install-update")
            n.add_button("Update Now", "app.install-update")
        elif APT_SOURCE.exists() and shutil.which("update-manager"):
            n.set_body("Install it from Software Updater.")
            n.set_default_action("app.open-updater")
            n.add_button("Open Software Updater", "app.open-updater")
        else:
            n.set_body("Download it from GitHub.")
            n.set_default_action(whats_new)
        n.add_button("What's New", whats_new)
        self.app.send_notification("update-available", n)


def _packagekit():
    try:
        import gi
        gi.require_version("PackageKitGlib", "1.0")
        from gi.repository import PackageKitGlib
        return PackageKitGlib
    except (ImportError, ValueError):
        return None


def _friendly_error(message):
    text = (message or "").lower()
    if "auth" in text or "polkit" in text or "not authorized" in text:
        return "The password wasn't given, so nothing was changed"
    if "lock" in text:
        return "Another program is installing updates. Try again when it's done"
    if "network" in text or "resolve" in text or "download" in text:
        return "Couldn't download the update. Check your connection"
    return message or "Unknown error"


def open_updater():
    if shutil.which("update-manager"):
        try:
            Gio.Subprocess.new(["update-manager"], Gio.SubprocessFlags.NONE)
            return
        except GLib.Error:
            pass
    Gio.AppInfo.launch_default_for_uri(DOWNLOAD_PAGE, None)
