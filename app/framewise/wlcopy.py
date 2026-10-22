"""Puts things on the clipboard with wl-copy, without ever making the app wait.

wl-copy keeps serving the clipboard after Framewise closes, which GTK's own
clipboard can't. But it can take a while (or hang until its time is up) when
it can't get the desktop's attention, so it runs on its own: the data goes to
it through a temporary file, and nothing waits for it to finish.
"""

import shutil
import subprocess
import tempfile

_running = []   # wl-copy processes still going; checked (and cleaned up) on each copy


def copy(data, mime=None):
    """data: bytes. True if wl-copy was started (it finishes on its own)."""
    if shutil.which("wl-copy") is None:
        return False
    _running[:] = [p for p in _running if p.poll() is None]
    try:
        with tempfile.TemporaryFile() as f:
            f.write(data)
            f.seek(0)
            cmd = ["wl-copy"] + (["--type", mime] if mime else [])
            _running.append(subprocess.Popen(cmd, stdin=f, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, start_new_session=True))
        return True
    except OSError:
        return False


def copy_file(path, mime):
    try:
        with open(path, "rb") as f:
            return copy(f.read(), mime)
    except OSError:
        return False
