"""Send to phone: a file, a picture or some text, to a phone on the same Wi-Fi;
and the other way: files and text from the phone to this computer.

A small web server runs on this computer while the Send to Phone window is
open (at most LIFETIME seconds). It answers only one secret address, made
of a random token nobody can guess; the QR code holds that address. The
phone gets a simple page with a Download button (or the text, with Copy).
Nothing goes through the internet.
"""

import html
import mimetypes
import os
import secrets
import shutil
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

    def __init__(self, item, on_event=None, lifetime=LIFETIME, host=None, bind=None):
        """host: the address put in the link (the local network one by default);
        bind: where the server listens: only that address by default, so it
        can't be reached through the computer's other networks (a VPN, a
        second network card). Tests use 127.0.0.1."""
        self.item = item
        self.on_event = on_event
        self.token = secrets.token_urlsafe(16)
        self.host = host or local_address()
        if self.host is None:
            raise OSError("This computer isn't connected to a network")
        self._server = ThreadingHTTPServer((bind or self.host, 0), self._handler())
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


# ---- receiving: files and text from the phone ---------------------------------------------

MAX_TEXT = 1_000_000            # bytes of text the phone can send at once
KEEP_FREE = 1_000_000_000       # bytes left free on the disk: bigger uploads are refused
PART = ".framewise-part"


def safe_name(name):
    """A plain file name from what the phone says, never a path: no folders,
    no hidden or control characters, not too long."""
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip().lstrip(".").strip()
    if len(name.encode()) > 150:
        stem, dot, ext = name.rpartition(".")
        if not dot or len(ext) > 10:
            stem, ext = name, ""
        while len((stem + "." + ext if ext else stem).encode()) > 150:
            stem = stem[:-1]
        name = f"{stem}.{ext}" if ext else stem
    return name or "File from phone"


def unique_path(folder, name):
    """folder/name, or "name (2).ext"... when that is taken (never overwrites)."""
    path = Path(folder) / name
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists() or path.with_name(path.name + PART).exists():
        path = Path(folder) / f"{stem} ({n}){suffix}"
        n += 1
    return path


class Receiving:
    """A server that takes files and text from a phone, for as long as the
    window is open (it stops after LIFETIME without anything arriving).
    Files are written into folder as they arrive and are never opened.
    on_event(kind, detail) runs on the main loop: ("opened", phone address),
    ("receiving", name), ("received", path), ("failed", name), ("text", text),
    ("stopped", None)."""

    def __init__(self, folder, on_event=None, lifetime=LIFETIME, host=None, bind=None):
        self.folder = Path(folder)
        self.on_event = on_event
        self.lifetime = lifetime
        self.token = secrets.token_urlsafe(16)
        self.host = host or local_address()
        if self.host is None:
            raise OSError("This computer isn't connected to a network")
        self._lock = threading.Lock()
        self._busy = 0
        self._last = time.monotonic()
        self.stopped = False
        self._server = ThreadingHTTPServer((bind or self.host, 0), self._handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._timer = None
        self._watch()

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/{self.token}/"

    @property
    def expires(self):
        return self._last + self.lifetime

    def _watch(self):
        if self.stopped:
            return
        if self._busy == 0 and time.monotonic() >= self.expires:
            self.stop()
            return
        self._timer = threading.Timer(min(30, self.lifetime), self._watch)
        self._timer.daemon = True
        self._timer.start()

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self._timer:
            self._timer.cancel()
        threading.Thread(target=self._server.shutdown, daemon=True).start()
        self._server.server_close()
        self._emit("stopped", None)

    def _emit(self, kind, detail):
        if self.on_event:
            from gi.repository import GLib
            GLib.idle_add(lambda: self.on_event(kind, detail) and False)

    def _handler(self):
        receiving = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Framewise"
            sys_version = ""

            def log_message(self, *_args):
                pass

            def do_GET(self):
                receiving._get(self)

            def do_PUT(self):
                receiving._put(self)

            def do_POST(self):
                receiving._post(self)

        return Handler

    def _route(self, req):
        """The part of the path after the token, or None (answered 404)."""
        base = f"/{self.token}/"
        path = urllib.parse.urlsplit(req.path).path
        if self.stopped or not path.startswith(base):
            req.send_error(404)
            return None
        return path[len(base):]

    def _only(self, req, rest):
        """True when the request is for <token>/rest; otherwise it is answered 404."""
        got = self._route(req)
        if got is None:
            return False
        if got != rest:
            req.send_error(404)
            return False
        return True

    def _get(self, req):
        if not self._only(req, ""):
            return
        data = UPLOAD_PAGE.encode()
        req.send_response(200)
        req.send_header("Content-Type", "text/html; charset=utf-8")
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Cache-Control", "no-store")
        req.send_header("Referrer-Policy", "no-referrer")
        req.end_headers()
        req.wfile.write(data)
        self._last = time.monotonic()
        self._emit("opened", req.client_address[0])

    def _length(self, req):
        try:
            n = int(req.headers.get("Content-Length", ""))
        except ValueError:
            return None
        return n if n >= 0 else None

    def _put(self, req):
        """PUT <token>/file?name=...: the file's bytes, as they are."""
        if not self._only(req, "file"):
            return
        size = self._length(req)
        if size is None:
            req.send_error(411)            # the size must be given up front
            return
        name = safe_name(urllib.parse.parse_qs(urllib.parse.urlsplit(req.path).query).get("name", [""])[0])
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.folder).free
        except OSError:
            req.send_error(500, "The folder for received files can't be used")
            return
        if size > free - KEEP_FREE:
            req.send_error(507, "Not enough space on the computer")
            return
        with self._lock:
            final = unique_path(self.folder, name)
            part = final.with_name(final.name + PART)
            try:
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except OSError:
                req.send_error(500)
                return
            self._busy += 1
        self._emit("receiving", final.name)
        ok = False
        try:
            with os.fdopen(fd, "wb") as f:
                left = size
                while left > 0:
                    chunk = req.rfile.read(min(CHUNK, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
                    self._last = time.monotonic()
            ok = left == 0
            if ok:
                with self._lock:
                    if final.exists():         # taken meanwhile: never overwrite
                        final = unique_path(self.folder, final.name)
                    os.rename(part, final)
        except OSError:
            ok = False
        finally:
            with self._lock:
                self._busy -= 1
            self._last = time.monotonic()
            if not ok:
                try:
                    part.unlink()
                except OSError:
                    pass
        if ok:
            req.send_response(200)
            req.send_header("Content-Length", "0")
            req.end_headers()
            self._emit("received", str(final))
        else:
            self._emit("failed", final.name)
            try:
                req.send_error(400, "The file didn't arrive whole")
            except OSError:
                pass

    def _post(self, req):
        """POST <token>/text: some text (UTF-8)."""
        if not self._only(req, "text"):
            return
        size = self._length(req)
        if size is None or size > MAX_TEXT:
            req.send_error(413 if size else 411)
            return
        text = req.rfile.read(size).decode("utf-8", "replace")
        self._last = time.monotonic()
        req.send_response(200)
        req.send_header("Content-Length", "0")
        req.end_headers()
        if text.strip():
            self._emit("text", text)


UPLOAD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send to your computer · Framewise</title><style>
body{font-family:system-ui,sans-serif;margin:0;padding:28px 20px;background:#f6f5f4;color:#222;text-align:center}
@media (prefers-color-scheme:dark){body{background:#1e1e22;color:#eee} textarea{background:#2a2a30;color:#eee}}
h1{font-size:15px;font-weight:600;opacity:.6;margin:0 0 20px}
.button,button{display:inline-block;background:#3584e4;color:#fff;border:0;border-radius:12px;
padding:14px 34px;font-size:17px;font-weight:600;text-decoration:none;cursor:pointer}
button{font-family:inherit} button.second{background:transparent;color:inherit;border:1px solid rgba(127,127,127,.5);padding:10px 24px;font-size:15px}
textarea{width:100%;box-sizing:border-box;height:20vh;font:16px/1.45 system-ui,sans-serif;border-radius:12px;padding:12px;
border:1px solid rgba(0,0,0,.15);margin:28px 0 10px}
ul{list-style:none;padding:0;margin:22px 0 0;text-align:left}
li{background:rgba(127,127,127,.12);border-radius:10px;padding:10px 12px;margin:8px 0;word-break:break-all}
li small{display:block;opacity:.65;margin-top:3px}
progress{width:100%;height:6px;margin-top:6px}
input{display:none}
</style></head><body><h1>To your computer, with Framewise</h1>
<label class="button" for="files">Choose Files</label>
<input id="files" type="file" multiple>
<ul id="list"></ul>
<textarea id="text" placeholder="Or type or paste text or a link"></textarea>
<button class="second" id="send-text">Send Text</button>
<script>
const list=document.getElementById('list');
function row(name){const li=document.createElement('li');li.textContent=name;
const s=document.createElement('small');li.appendChild(s);const p=document.createElement('progress');
p.max=1;p.value=0;li.appendChild(p);list.appendChild(li);return {s,p};}
let queue=Promise.resolve();
document.getElementById('files').addEventListener('change',e=>{
for(const f of e.target.files){const r=row(f.name);r.s.textContent='Waiting…';
queue=queue.then(()=>new Promise(done=>{const x=new XMLHttpRequest();
x.open('PUT','file?name='+encodeURIComponent(f.name));
x.upload.onprogress=ev=>{if(ev.lengthComputable){r.p.value=ev.loaded/ev.total;
r.s.textContent=Math.round(100*ev.loaded/ev.total)+'%';}};
x.onload=()=>{r.p.remove();r.s.textContent=x.status==200?'Sent ✓':(x.status==507?'Not enough space on the computer':'Not sent');done();};
x.onerror=()=>{r.p.remove();r.s.textContent='Not sent: the computer can’t be reached';done();};
r.s.textContent='Sending…';x.send(f);}));}
e.target.value='';});
document.getElementById('send-text').addEventListener('click',()=>{
const t=document.getElementById('text');if(!t.value.trim())return;const r=row('Text');
fetch('text',{method:'POST',body:t.value}).then(x=>{r.p.remove();
r.s.textContent=x.ok?'Sent ✓ (on the computer’s clipboard)':'Not sent';if(x.ok)t.value='';})
.catch(()=>{r.p.remove();r.s.textContent='Not sent: the computer can’t be reached';});});
</script></body></html>"""


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
