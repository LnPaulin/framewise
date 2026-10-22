"""Small JSON stores for settings and remembered state.

settings.json holds what the user chooses; state.json holds what the app
remembers on its own (like the portal restore token).
"""

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "framewise"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "framewise"
OLD_NAME = "capturekit"     # what Framewise was called before; its folders are moved over once


def _move_old_folders():
    """Moves settings and data from the old name, fixing paths saved inside them."""
    moved = []
    for new in (CONFIG_DIR, DATA_DIR):
        old = new.with_name(OLD_NAME)
        if old.is_dir() and not new.exists():
            try:
                old.rename(new)
                moved.append((old, new))
            except OSError:
                pass
    for old, new in moved:
        for path in new.rglob("*.json"):
            try:
                if path.stat().st_size > 5_000_000:
                    continue
                text = path.read_text()
                if str(old) in text:
                    path.write_text(text.replace(f"{old}/", f"{new}/").replace(f'"{old}"', f'"{new}"'))
            except (OSError, UnicodeDecodeError):
                pass


_move_old_folders()

DEFAULT_SETTINGS = {
    "countdown": 3,               # seconds, 0 = off
    "fps": 60,
    "quality": "high",            # small | balanced | high | max
    "system_audio": True,
    "microphone": True,
    "microphone_device": "",      # "" = system default
    "camera": False,              # open the camera bubble with the record window
    "open_editor": True,          # open the editor after recording (else save straight away)
    "folder": str(Path.home() / "Videos" / "Recordings"),
    "last_mode": "screen",        # area | window | screen
    "last_area": None,            # [x, y, w, h] in screen pixels
    # Screenshots
    "shot_folder": str(Path.home() / "Pictures" / "Screenshots"),
    "shot_auto_copy": True,       # every screenshot goes to the clipboard
    "shot_pointer": False,        # include the mouse pointer
    "shot_loupe": True,           # magnifier while choosing
    # Always there
    "topbar_icon": True,          # the Framewise menu at the top right (read by the extension)
    # Updates (updates.py)
    "check_updates": True,        # ask GitHub about once a day for a newer version
    "last_update_check": 0,       # time.time() of the last check
    "update_notified": "",        # the newer version already announced
    "last_run_version": "",       # to say "updated to X" once after an update
    # Clipboard history (kept by the GNOME Shell extension, which reads these)
    "clipboard_history": True,
    "clipboard_limit": 100,       # unpinned entries kept; 0 = no limit
    "clipboard_images": True,     # keep pictures and copied files too
    "clipboard_retention_days": 0,   # 0 = until the limit pushes them out
    "clipboard_skip_sensitive": False,   # skip text that looks like a password or key
}


class _Store:
    def __init__(self, path, defaults):
        self.path = path
        self.defaults = defaults
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, key):
        data = self._load()
        if key in data:
            return data[key]
        return self.defaults.get(key)

    def set(self, key, value):
        data = self._load()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)


settings = _Store(CONFIG_DIR / "settings.json", DEFAULT_SETTINGS)
_state = _Store(CONFIG_DIR / "state.json", {})


def get(key):
    return _state.get(key)


def set(key, value):  # noqa: A001
    _state.set(key, value)
