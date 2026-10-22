"""Screenshots: freeze the screens, choose, then save, copy and preview.

With the GNOME Shell extension (0.7+), the desktop is captured without any
dialog, windows can be highlighted and taken on their own, and a small
preview card appears in the corner with Edit, Pin and Delete. Without it,
the desktop portal takes the picture (GNOME may ask for permission once)
and a notification offers the same actions.
"""

import shutil
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

from .state import DATA_DIR, settings  # noqa: E402

WORK_DIR = DATA_DIR / "shots"
PREVIEW_SECONDS = 6
DEFAULT_FOLDER = str(Path.home() / "Pictures" / "Screenshots")


def shot_destination(folder=None, suffix=".png", prefix="Screenshot"):
    folder = Path(folder or settings.get("shot_folder") or DEFAULT_FOLDER).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path.home() / "Pictures"
        folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d at %H.%M.%S")
    path = folder / f"{prefix} {stamp}{suffix}"
    n = 2
    while path.exists():
        path = folder / f"{prefix} {stamp} ({n}){suffix}"
        n += 1
    return path


def copy_image(path):
    """Puts the PNG on the clipboard; wl-copy keeps it there after the app closes."""
    from . import wlcopy
    return wlcopy.copy_file(path, "image/png")


def crop_png(src, dest, rect, ratio, origin=(0, 0)):
    """Cuts rect (desktop coordinates) out of the desktop picture src."""
    pix = GdkPixbuf.Pixbuf.new_from_file(str(src))
    x, y, w, h = rect
    px = max(0, round((x - origin[0]) * ratio))
    py = max(0, round((y - origin[1]) * ratio))
    pw = max(1, min(round(w * ratio), pix.get_width() - px))
    ph = max(1, min(round(h * ratio), pix.get_height() - py))
    pix.new_subpixbuf(px, py, pw, ph).savev(str(dest), "png", [], [])
    return pw, ph


class ShotSession:
    """One screenshot, from freezing the screen to the preview card."""

    def __init__(self, app):
        self.app = app
        self.shell = app.session.shell
        self.busy = False            # between the shortcut and the saved file
        self.card = False            # the preview card is showing (app kept running)
        self.last = None             # path of the latest screenshot
        self.text_mode = False       # copying the text in an area, not a picture
        self.shell.on_shot_action = self._on_card

    def start(self, whole_screen=False, delay=0, text=False):
        if self.busy:
            return
        self.text_mode = text
        if self.card:
            # A new screenshot replaces the card of the last one.
            self.card = False
            self.shell.hide_shot()
            self.app.release()
        self.busy = True
        self.app.hold()
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if delay > 0:
            from .shot_select import monitors
            from .shell import ShellCountdown
            if self.shell.has_countdown:
                ShellCountdown(self.shell, monitors()[0][0], int(delay),
                               lambda ok: self._freeze(whole_screen) if ok else self._done())
                return
            GLib.timeout_add(int(delay * 1000), lambda: (self._freeze(whole_screen), False)[1])
            return
        self._freeze(whole_screen)

    # ---- capturing ---------------------------------------------------------------------

    def _freeze(self, whole_screen):
        still = WORK_DIR / "desktop.png"
        still.unlink(missing_ok=True)
        cursor = bool(settings.get("shot_pointer"))
        if self.shell.has_screenshots:
            windows = self.shell.list_windows()
            if self.shell.screenshot(still, cursor):
                self._choose(still, windows, whole_screen)
                return
        self._portal_still(still, whole_screen)

    def _portal_still(self, still, whole_screen):
        gi.require_version("Xdp", "1.0")
        from gi.repository import Xdp
        from .portal import _get_portal

        def done(portal, result):
            try:
                uri = portal.take_screenshot_finish(result)
                shutil.copy(Gio.File.new_for_uri(uri).get_path(), still)
            except (GLib.Error, OSError, TypeError) as e:
                self.app.notify("The screen could not be captured", str(e))
                self._done()
                return
            self._choose(still, [], whole_screen)

        _get_portal().take_screenshot(None, Xdp.ScreenshotFlags.NONE, None, done)

    def _choose(self, still, windows, whole_screen):
        from .shot_select import ShotSelector, desktop_bounds, monitors
        if whole_screen:
            self._chosen(still, ("screen", monitors()[0][1]), desktop_bounds())
            return
        from .shot_select import TEXT_HINT
        bounds = desktop_bounds()
        ShotSelector(self.app, still, windows,
                     lambda result: self._chosen(still, result, bounds),
                     hint=TEXT_HINT if self.text_mode else None)

    def _chosen(self, still, result, bounds):
        if result is None:
            self._done()
            return
        kind, what = result
        pix_w = GdkPixbuf.Pixbuf.get_file_info(str(still))[1]
        ratio = pix_w / bounds[2]
        if self.text_mode:
            self._read_text(still, result, ratio, bounds)
            return
        dest = shot_destination()
        size = None
        if kind == "window" and self.shell.has_screenshots:
            if self.shell.screenshot_window(what["id"], dest, bool(settings.get("shot_pointer"))):
                info = GdkPixbuf.Pixbuf.get_file_info(str(dest))
                size = (info[1], info[2])
        if size is None:
            rect = (what["x"], what["y"], what["w"], what["h"]) if kind == "window" else what
            size = crop_png(still, dest, rect, ratio, bounds[:2])
        at = (what["x"], what["y"]) if kind == "window" else what[:2]
        still.unlink(missing_ok=True)
        self._finished(dest, size, at)

    def _read_text(self, still, result, ratio, bounds):
        import cairo
        from . import ocr
        kind, what = result
        rect = (what["x"], what["y"], what["w"], what["h"]) if kind == "window" else what
        area = WORK_DIR / "text.png"
        crop_png(still, area, rect, ratio, bounds[:2])
        still.unlink(missing_ok=True)
        surface = cairo.ImageSurface.create_from_png(str(area))
        area.unlink(missing_ok=True)

        def done(text, error):
            if error:
                self.app.notify("Text recognition failed", error)
            elif not text:
                self.app.notify("No text found", "Try a larger area or zoom in on the text.")
            elif ocr.copy_text(text):
                lines = text.count("\n") + 1
                self.app.notify(f"Copied {lines} line{'s' if lines != 1 else ''} of text", ocr.preview(text))
            else:
                self.app.notify("Couldn't copy the text", "wl-copy (package wl-clipboard) is not installed")
            self._done()

        ocr.read_text_async(surface, done)

    def _finished(self, path, size, at):
        self.last = path
        copied = copy_image(path) if settings.get("shot_auto_copy") else False
        note = f"Saved to {path.parent.name}" + (" and copied" if copied else "")
        if self.shell.has_screenshots:
            self.shell.show_shot(path, size[0], size[1], at[0], at[1], PREVIEW_SECONDS, note)
            # The card answers through _on_card; keep running until then.
            self.card = True
            self.app.hold()
        else:
            self._notify(path, note)
        self._done()

    def _notify(self, path, note):
        n = Gio.Notification.new("Screenshot taken")
        n.set_body(note)
        target = GLib.Variant("s", str(path))
        n.set_default_action(Gio.Action.print_detailed_name("app.shot-edit", target))
        n.add_button("Edit", Gio.Action.print_detailed_name("app.shot-edit", target))
        n.add_button("Pin", Gio.Action.print_detailed_name("app.shot-pin", target))
        n.add_button("Show in folder", Gio.Action.print_detailed_name(
            "app.open-file", GLib.Variant("s", str(path.parent))))
        self.app.send_notification("screenshot", n)

    def _on_card(self, action):
        if not self.card:
            return
        self.card = False
        path = self.last
        if path and action == "edit":
            self.app.edit_shot(str(path))
        elif path and action == "pin":
            self.app.pin_shot(str(path))
        elif path and action == "delete":
            try:
                Gio.File.new_for_path(str(path)).trash(None)
            except GLib.Error:
                path.unlink(missing_ok=True)
        self.app.release()

    def _done(self):
        if self.busy:
            self.busy = False
            self.app.release()
