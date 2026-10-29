"""Send to phone: a file, a picture or some text, to a phone on the same Wi-Fi.

A small web server runs on this computer while the Send to Phone window is
open (at most LIFETIME seconds). It answers only one secret address, made
of a random token nobody can guess; the QR code holds that address. The
phone gets a simple page with a Download button (or the text, with Copy).
Nothing goes through the internet.
"""

import html
import mimetypes
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIFETIME = 15 * 60       # seconds the address works
CHUNK = 256 * 1024


def local_address():
    """This computer's address on the local network, or None (no network).
    Nothing is sent: a UDP "connection" only picks the outgoing interface."""
    for probe in ("192.168.1.1", "10.255.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    return None


def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


class Item:
    """What is being sent: a file (path) or text."""

    def __init__(self, path=None, text=None, name=None):
        self.path = Path(path) if path else None
        self.text = text
        self.name = name or (self.path.name if self.path else "Text")

    @property
    def size(self):
        return self.path.stat().st_size if self.path else len(self.text.encode())

    @property
    def mime(self):
        if self.path is None:
            return "text/plain; charset=utf-8"
        return mimetypes.guess_type(self.name)[0] or "application/octet-stream"


class Sharing:
    """The server for one item. on_event(kind, detail) runs on the main loop:
    ("opened", phone address), ("downloaded", phone address), ("stopped", None)."""

    def __init__(self, item, on_event=None, lifetime=LIFETIME, host=None, bind="0.0.0.0"):
        """host: the address put in the link (the local network one by default);
        bind: where the server listens (tests use 127.0.0.1)."""
        self.item = item
        self.on_event = on_event
        self.token = secrets.token_urlsafe(16)
        self.host = host or local_address()
        if self.host is None:
            raise OSError("This computer isn't connected to a network")
        self._server = ThreadingHTTPServer((bind, 0), self._handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.expires = time.monotonic() + lifetime
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._timer = threading.Timer(lifetime, self.stop)
        self._timer.daemon = True
        self._timer.start()
        self.stopped = False

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/{self.token}/"

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self._timer.cancel()
        threading.Thread(target=self._server.shutdown, daemon=True).start()
        self._server.server_close()
        self._emit("stopped", None)

    def _emit(self, kind, detail):
        if self.on_event:
            from gi.repository import GLib
            GLib.idle_add(lambda: self.on_event(kind, detail) and False)

    # ---- the web pages --------------------------------------------------------------

    def _handler(self):
        sharing = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Framewise"
            sys_version = ""

            def log_message(self, *_args):
                pass                     # quiet: nothing about the phone is logged

            def do_GET(self):
                sharing._serve(self, head=False)

            def do_HEAD(self):
                sharing._serve(self, head=True)

        return Handler

    def _serve(self, req, head):
        base = f"/{self.token}/"
        path = urllib.parse.urlsplit(req.path).path
        if self.stopped or not path.startswith(base):
            req.send_error(404)
            return
        rest = path[len(base):]
        if rest == "":
            self._page(req, head)
            if not head:
                self._emit("opened", req.client_address[0])
        elif rest == "file":
            self._file(req, head)
        else:
            req.send_error(404)

    def _page(self, req, head):
        item = self.item
        name = html.escape(item.name)
        if item.path is None:
            body = (f'<textarea id="t" readonly>{html.escape(item.text)}</textarea>'
                    '<button onclick="copy()">Copy</button>'
                    '<a class="small" href="file" download="text.txt">Save as a file</a>')
        else:
            preview = '<img src="file" alt="">' if item.mime.startswith("image/") else ""
            body = (f'{preview}<p class="name">{name}</p><p class="size">{human_size(item.size)}</p>'
                    '<a class="button" href="file?download=1">Download</a>')
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · Framewise</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:28px 20px;background:#f6f5f4;color:#222;text-align:center}}
@media (prefers-color-scheme:dark){{body{{background:#1e1e22;color:#eee}} textarea{{background:#2a2a30;color:#eee}}}}
h1{{font-size:15px;font-weight:600;opacity:.6;margin:0 0 20px}}
img{{max-width:100%;max-height:50vh;border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.2)}}
.name{{font-size:18px;font-weight:600;word-break:break-all;margin:18px 0 4px}} .size{{opacity:.6;margin:0 0 22px}}
.button,button{{display:inline-block;background:#3584e4;color:#fff;border:0;border-radius:12px;
padding:14px 34px;font-size:17px;font-weight:600;text-decoration:none}}
textarea{{width:100%;box-sizing:border-box;height:45vh;font:16px/1.45 system-ui,sans-serif;border-radius:12px;padding:12px;
border:1px solid rgba(0,0,0,.15);margin-bottom:16px}}
.small{{display:block;margin-top:16px;opacity:.7;color:inherit}}
</style></head><body><h1>From Framewise</h1>{body}
<script>function copy(){{const t=document.getElementById('t');t.select();
(navigator.clipboard?navigator.clipboard.writeText(t.value):Promise.reject()).catch(()=>document.execCommand('copy'));
document.querySelector('button').textContent='Copied';}}</script></body></html>"""
        data = page.encode()
        req.send_response(200)
        req.send_header("Content-Type", "text/html; charset=utf-8")
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Cache-Control", "no-store")
        req.send_header("Referrer-Policy", "no-referrer")
        req.end_headers()
        if not head:
            req.wfile.write(data)

    def _file(self, req, head):
        item = self.item
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.path).query)
        attach = "download" in query or item.path is None
        quoted = urllib.parse.quote(item.name if item.path else "text.txt")
        req.send_response(200)
        req.send_header("Content-Type", item.mime)
        req.send_header("Content-Length", str(item.size))
        req.send_header("Cache-Control", "no-store")
        req.send_header("Content-Disposition",
                        f"{'attachment' if attach else 'inline'}; filename*=UTF-8''{quoted}")
        req.end_headers()
        if head:
            return
        try:
            if item.path is None:
                req.wfile.write(item.text.encode())
            else:
                with open(item.path, "rb") as f:
                    while chunk := f.read(CHUNK):
                        req.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return                      # the phone stopped (or only peeked)
        if attach:
            self._emit("downloaded", req.client_address[0])


def qr_texture(text, size=320):
    """A Gdk.Texture of a QR code for text, or None without OpenCV."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    if not hasattr(cv2, "QRCodeEncoder"):
        return None
    img = cv2.QRCodeEncoder.create().encode(text)
    if img is None or not img.size:
        return None
    border = 4
    img = np.pad(img, border, constant_values=255)
    scale = max(1, size // img.shape[0])
    img = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
    rgb = np.ascontiguousarray(np.stack([img] * 3, axis=-1).astype(np.uint8))
    import gi
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib
    h, w = rgb.shape[:2]
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(rgb.tobytes()), w * 3)
