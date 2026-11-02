import http.client
import shutil
import socket
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from framewise import share


class NameTests(unittest.TestCase):
    def test_plain_names_kept(self):
        self.assertEqual(share.safe_name("IMG_2041.jpg"), "IMG_2041.jpg")
        self.assertEqual(share.safe_name("Vacances été.mov"), "Vacances été.mov")

    def test_never_a_path(self):
        for bad in ("../../.bashrc", "/etc/passwd", "a/b/../../c.txt", "..\\..\\x.exe", "~/.ssh/id_rsa"):
            name = share.safe_name(bad)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertFalse(name.startswith("."), name)
            self.assertNotEqual(name, "..")

    def test_empty_and_odd(self):
        self.assertEqual(share.safe_name(""), "File from phone")
        self.assertEqual(share.safe_name("..."), "File from phone")
        self.assertEqual(share.safe_name("a\x00b\nc.txt"), "abc.txt")

    def test_long_names_cut_keep_extension(self):
        name = share.safe_name("x" * 400 + ".jpeg")
        self.assertLessEqual(len(name.encode()), 150)
        self.assertTrue(name.endswith(".jpeg"))

    def test_unique_never_overwrites(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "a.jpg").write_text("x")
        self.assertEqual(share.unique_path(d, "a.jpg").name, "a (2).jpg")
        self.assertEqual(share.unique_path(d, "b.jpg").name, "b.jpg")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.r = share.Receiving(self.dir / "in", host="127.0.0.1", bind="127.0.0.1")
        self.addCleanup(self.r.stop)

    def _request(self, method, path, body=None, headers=None, token=None):
        c = http.client.HTTPConnection("127.0.0.1", self.r.port, timeout=10)
        c.request(method, f"/{token or self.r.token}/{path}", body=body, headers=headers or {})
        res = c.getresponse()
        data = res.read()
        c.close()
        return res.status, data

    def _put(self, name, data):
        return self._request("PUT", "file?name=" + urllib.parse.quote(name), body=data)[0]

    def test_page(self):
        status, data = self._request("GET", "")
        self.assertEqual(status, 200)
        self.assertIn(b"Choose Files", data)

    def test_wrong_token_or_path(self):
        self.assertEqual(self._request("GET", "", token="nope")[0], 404)
        self.assertEqual(self._request("PUT", "file?name=a", body=b"x", token="nope")[0], 404)
        self.assertEqual(self._request("GET", "other")[0], 404)
        self.assertFalse((self.dir / "in").exists() and any((self.dir / "in").iterdir()))

    def test_file_arrives(self):
        data = bytes(range(256)) * 5000
        self.assertEqual(self._put("photo.jpg", data), 200)
        self.assertEqual((self.dir / "in" / "photo.jpg").read_bytes(), data)

    def test_same_name_twice(self):
        self._put("a.txt", b"one")
        self._put("a.txt", b"two")
        self.assertEqual((self.dir / "in" / "a.txt").read_bytes(), b"one")
        self.assertEqual((self.dir / "in" / "a (2).txt").read_bytes(), b"two")

    def test_path_in_name_stays_in_folder(self):
        self.assertEqual(self._put("../../escape.txt", b"x"), 200)
        self.assertFalse((self.dir / "escape.txt").exists())
        self.assertTrue((self.dir / "in" / "escape.txt").exists())

    def test_size_required(self):
        s = socket.create_connection(("127.0.0.1", self.r.port), timeout=5)
        s.sendall(f"PUT /{self.r.token}/file?name=a HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                  "1\r\nx\r\n0\r\n\r\n".encode())
        self.assertIn(b" 411 ", s.recv(200))
        s.close()

    def test_full_disk_refused(self):
        with mock.patch.object(share.shutil, "disk_usage", return_value=mock.Mock(free=share.KEEP_FREE + 10)):
            self.assertEqual(self._put("big.bin", b"x" * 100), 507)
        self.assertFalse((self.dir / "in" / "big.bin").exists())

    def test_cut_off_upload_leaves_nothing(self):
        s = socket.create_connection(("127.0.0.1", self.r.port), timeout=5)
        s.sendall(f"PUT /{self.r.token}/file?name=cut.mov HTTP/1.1\r\nHost: x\r\n"
                  "Content-Length: 100000\r\n\r\n".encode() + b"x" * 1000)
        s.shutdown(socket.SHUT_WR)
        s.recv(200)
        s.close()
        import time
        time.sleep(0.3)
        self.assertEqual(list((self.dir / "in").iterdir()), [])

    def test_text(self):
        got = []
        self.r.on_event = None
        with mock.patch.object(self.r, "_emit", side_effect=lambda k, d: got.append((k, d))):
            status, _ = self._request("POST", "text", body="héllo https://x.org".encode())
        self.assertEqual(status, 200)
        self.assertIn(("text", "héllo https://x.org"), got)

    def test_text_too_long(self):
        status, _ = self._request("POST", "text", body=b"x" * (share.MAX_TEXT + 1))
        self.assertEqual(status, 413)

    def test_stopped_refuses(self):
        self.r.stop()
        with self.assertRaises(OSError):
            self._put("late.txt", b"x")


class ExpiryTests(unittest.TestCase):
    def test_stops_when_idle(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        r = share.Receiving(d, host="127.0.0.1", bind="127.0.0.1", lifetime=0.3)
        self.addCleanup(r.stop)
        import time
        time.sleep(0.8)
        self.assertTrue(r.stopped)


if __name__ == "__main__":
    unittest.main()
