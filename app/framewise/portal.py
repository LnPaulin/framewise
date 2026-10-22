"""Screen sharing through xdg-desktop-portal (the "share your screen" dialog).

On Wayland an app can only see the screen through the portal. It shows the
system dialog, hands back a PipeWire stream, and can give a restore token so
the same choice is reused next time without asking.
"""

from dataclasses import dataclass

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Gio, GLib, Xdp  # noqa: E402

from . import state  # noqa: E402


@dataclass
class Stream:
    node_id: int
    width: int
    height: int
    x: int
    y: int
    source_type: int  # 1 = monitor, 2 = window


@dataclass
class Capture:
    """An open screen-share: the PipeWire connection plus what it shows."""

    session: Xdp.Session
    fd: int
    stream: Stream

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


class PortalError(Exception):
    pass


_portal = None


def _get_portal():
    global _portal
    if _portal is None:
        _portal = Xdp.Portal.new()
    return _portal


def _parse_streams(variant):
    streams = []
    for node_id, props in variant.unpack():
        size = props.get("size", (0, 0))
        pos = props.get("position", (0, 0))
        streams.append(Stream(node_id, size[0], size[1], pos[0], pos[1],
                              props.get("source_type", 0)))
    return streams


def _tokens():
    return dict(state.get("restore_tokens") or {})


def _save_token(screen, token):
    tokens = _tokens()
    if token:
        tokens[screen] = token
    else:
        tokens.pop(screen, None)
    state.set("restore_tokens", tokens)


def open_capture(kind, cursor, on_ready, on_error, on_closed=None, screen=None,
                 screen_of_stream=None):
    """Ask the portal for a screen ("monitor") or a window ("window").

    cursor: "embedded" draws the pointer into the video, "hidden" leaves it out.
    screen: which monitor is wanted (its connector, like "HDMI-1"). Each
    monitor keeps its own restore token, so the portal asks once per screen
    and then reuses that answer. screen_of_stream(stream) names the monitor a
    stream shows, so the token is filed under the screen actually picked.
    A window is asked for every time, because the window you want changes.
    Calls on_ready(Capture) or on_error(message) on the main loop.
    """
    portal = _get_portal()
    outputs = Xdp.OutputType.MONITOR if kind == "monitor" else Xdp.OutputType.WINDOW
    cursor_mode = {"embedded": Xdp.CursorMode.EMBEDDED,
                   "hidden": Xdp.CursorMode.HIDDEN,
                   "metadata": Xdp.CursorMode.METADATA}[cursor]
    token = _tokens().get(screen) if kind == "monitor" and screen else None
    persist = Xdp.PersistMode.PERSISTENT if kind == "monitor" else Xdp.PersistMode.NONE

    def on_started(session, result):
        try:
            if not session.start_finish(result):
                raise PortalError("cancelled")
        except GLib.Error as e:
            # A stale token can fail; forget it so the next try asks again.
            if token:
                _save_token(screen, None)
            on_error("cancelled" if "cancel" in e.message.lower() else e.message)
            return
        except PortalError as e:
            on_error(str(e))
            return
        streams = _parse_streams(session.get_streams())
        if kind == "monitor" and streams:
            new_token = session.get_restore_token()
            picked = screen_of_stream(streams[0]) if screen_of_stream else screen
            if new_token and picked:
                _save_token(picked, new_token)
        if not streams:
            session.close()
            on_error("The portal returned no screen")
            return
        fd = session.open_pipewire_remote()
        cap = Capture(session, fd, streams[0])
        if on_closed:
            session.connect("closed", lambda *_: on_closed())
        on_ready(cap)

    def on_created(_portal, result):
        try:
            session = portal.create_screencast_session_finish(result)
        except GLib.Error as e:
            if token:
                _save_token(screen, None)
            on_error(e.message)
            return
        session.start(None, None, on_started)

    portal.create_screencast_session(outputs, Xdp.ScreencastFlags.NONE, cursor_mode,
                                     persist, token, None, on_created)


def forget_screen(screen=None):
    """Forget a screen's permission (or every screen's) so the portal asks again."""
    if screen:
        _save_token(screen, None)
    else:
        state.set("restore_tokens", {})


def take_screenshot(on_ready, on_error):
    """Full-screen screenshot without the dialog. Gives back a file path."""
    portal = _get_portal()

    def done(_p, result):
        try:
            uri = portal.take_screenshot_finish(result)
        except GLib.Error as e:
            on_error(e.message)
            return
        on_ready(Gio.File.new_for_uri(uri).get_path())

    portal.take_screenshot(None, Xdp.ScreenshotFlags.NONE, None, done)
