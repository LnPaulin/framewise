import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from gi.repository import GLib

from framewise import share


def get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


def run_loop(seconds=0.3):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / "Holiday photo é.jpg"
        self.file.write_bytes(b"\xff\xd8" + b"x" * 300_000)
        self.events = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, item, **kw):
        s = share.Sharing(item, lambda k, d: self.events.append(k), host="127.0.0.1", bind="127.0.0.1", **kw)
        self.addCleanup(s.stop)
        return s

    def test_file_page_and_download(self):
        s = self.start(share.Item(path=self.file))
        status, headers, body = get(s.url)
        self.assertEqual(status, 200)
        self.assertIn(b"Holiday photo", body)
        self.assertIn(b"Download", body)
        status, headers, body = get(s.url + "file?download=1")
        self.assertEqual(body, self.file.read_bytes())
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''Holiday%20photo%20%C3%A9.jpg", headers["Content-Disposition"])
        run_loop()
        self.assertEqual(self.events, ["opened", "downloaded"])

    def test_wrong_token_or_path(self):
        s = self.start(share.Item(path=self.file))
        for url in (f"http://127.0.0.1:{s.port}/", f"http://127.0.0.1:{s.port}/nope/file",
                    s.url + "../etc/passwd", s.url + "other", f"http://127.0.0.1:{s.port}/{s.token}"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                get(url)
            self.assertEqual(e.exception.code, 404, url)

    def test_token_is_long_and_random(self):
        a = self.start(share.Item(path=self.file))
        b = self.start(share.Item(path=self.file))
        self.assertGreaterEqual(len(a.token), 20)
        self.assertNotEqual(a.token, b.token)

    def test_text_is_escaped(self):
        s = self.start(share.Item(text="<script>alert(1)</script> & hello", name="Text"))
        _status, _h, body = get(s.url)
        self.assertNotIn(b"<script>alert(1)", body)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt; &amp; hello", body)
        _status, headers, body = get(s.url + "file")
        self.assertEqual(body, b"<script>alert(1)</script> & hello")
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_head_sends_no_body(self):
        s = self.start(share.Item(path=self.file))
        status, headers, body = get(s.url + "file", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["Content-Length"], str(self.file.stat().st_size))
        run_loop()
        self.assertNotIn("downloaded", self.events)

    def test_stops(self):
        s = self.start(share.Item(path=self.file))
        url = s.url
        s.stop()
        time.sleep(0.2)
        with self.assertRaises(OSError):
            get(url)
        run_loop()
        self.assertIn("stopped", self.events)

    def test_expires(self):
        s = self.start(share.Item(path=self.file), lifetime=0.3)
        time.sleep(0.6)
        with self.assertRaises(OSError):
            get(s.url)

    def test_local_address(self):
        ip = share.local_address()
        if ip is not None:
            self.assertFalse(ip.startswith("127."))

    def test_qr_round_trip(self):
        texture = share.qr_texture("http://192.168.0.5:4242/abcdef/")
        if texture is None:
            self.skipTest("OpenCV not installed")
        path = self.dir / "qr.png"
        texture.save_to_png(str(path))
        from framewise import qr
        self.assertEqual(qr.read_codes(path), ["http://192.168.0.5:4242/abcdef/"])


if __name__ == "__main__":
    unittest.main()
