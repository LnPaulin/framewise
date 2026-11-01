import tempfile
import unittest
from pathlib import Path

from framewise import qr

try:
    import cv2
    import numpy as np
    HAVE = hasattr(cv2, "QRCodeEncoder")
except ImportError:
    HAVE = False


def code(text, module=6):
    """A QR code picture (BGR), each square `module` pixels wide."""
    img = cv2.QRCodeEncoder.create().encode(text)
    img = cv2.resize(img, None, fx=module, fy=module, interpolation=cv2.INTER_NEAREST)
    img = cv2.copyMakeBorder(img, 4 * module, 4 * module, 4 * module, 4 * module,
                             cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


@unittest.skipUnless(HAVE, "OpenCV not installed")
class ReadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def save(self, img):
        path = self.dir / "area.png"
        cv2.imwrite(str(path), img)
        return path

    def test_one_link(self):
        page = np.full((500, 700, 3), 240, np.uint8)
        c = code("https://github.com/LnPaulin/framewise")
        page[100:100 + c.shape[0], 200:200 + c.shape[1]] = c
        cv2.putText(page, "Scan me", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (30, 30, 30), 2)
        self.assertEqual(qr.read_codes(self.save(page)), ["https://github.com/LnPaulin/framewise"])

    def test_small_code_on_screen(self):
        # Two pixels per square, as in a corner of a web page.
        c = code("WIFI:S:Home;T:WPA;P:secret;;", module=2)
        self.assertEqual(qr.read_codes(self.save(c)), ["WIFI:S:Home;T:WPA;P:secret;;"])

    def test_two_codes_in_reading_order(self):
        page = np.full((400, 900, 3), 255, np.uint8)
        a, b = code("left"), code("right")
        page[50:50 + a.shape[0], 30:30 + a.shape[1]] = a
        page[50:50 + b.shape[0], 500:500 + b.shape[1]] = b
        found = qr.read_codes(self.save(page))
        if found == ["left"] or found == ["right"]:
            self.skipTest("this OpenCV finds one code at a time")
        self.assertEqual(found, ["left", "right"])

    def zoomed_out(self, text, pixels):
        """A code drawn the way a zoomed-out page shows it: smoothed, about
        `pixels` screen pixels per square."""
        c = code(text, module=16)
        side = int(c.shape[0] / 16 * pixels)
        return cv2.resize(c, (side, side), interpolation=cv2.INTER_AREA)

    def test_tiny_code_in_a_large_area(self):
        # 2b.3: a page zoomed out until the code is tiny, in a big selection.
        page = np.full((800, 1280, 3), 255, np.uint8)
        for y in range(16, 800, 22):
            cv2.putText(page, "Lorem ipsum dolor sit amet " * 3, (10, y), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, (60, 60, 60), 1, cv2.LINE_AA)
        c = self.zoomed_out("https://example.com/a", 1.4)
        page[100:100 + c.shape[0], 100:100 + c.shape[1]] = c
        self.assertEqual(qr.read_codes(self.save(page)), ["https://example.com/a"])

    def test_three_small_codes(self):
        # 2b.4: every code is found, even when one reader alone misses some.
        if not hasattr(cv2, "wechat_qrcode_WeChatQRCode"):
            self.skipTest("needs OpenCV's WeChat reader")
        page = np.full((800, 1280, 3), 255, np.uint8)
        for text, px, (x, y) in [("WIFI:S:home;T:WPA;P:pw;;", 3, (50, 50)),
                                 ("https://example.com/b", 2, (600, 300)), ("hello", 5, (900, 500))]:
            c = self.zoomed_out(text, px)
            page[y:y + c.shape[0], x:x + c.shape[1]] = c
        self.assertEqual(qr.read_codes(self.save(page)),
                         ["WIFI:S:home;T:WPA;P:pw;;", "https://example.com/b", "hello"])

    def test_no_code(self):
        page = np.full((300, 400, 3), 255, np.uint8)
        cv2.putText(page, "Just text", (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 3)
        self.assertEqual(qr.read_codes(self.save(page)), [])

    def test_missing_file(self):
        self.assertEqual(qr.read_codes(self.dir / "nope.png"), [])


class LinkTests(unittest.TestCase):
    def test_only_web_links(self):
        self.assertTrue(qr.is_link("https://example.com/a?b=c"))
        self.assertTrue(qr.is_link("  http://example.com  "))
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x", "example.com",
                    "https://example.com and more", "smb://server/share"):
            self.assertFalse(qr.is_link(bad), bad)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE, "OpenCV not installed")
class TextCaptureTests(unittest.TestCase):
    """Text Capture on an area holding a QR code: the code is copied, not OCR text."""

    def test_flow(self):
        from unittest import mock
        from gi.repository import GLib
        from framewise import shots

        tmp = Path(tempfile.mkdtemp())
        page = np.full((400, 600, 3), 250, np.uint8)
        c = code("https://example.com/hello")
        page[60:60 + c.shape[0], 150:150 + c.shape[1]] = c
        still = tmp / "desktop.png"
        cv2.imwrite(str(still), page)

        sent, loop = [], GLib.MainLoop()
        app = mock.Mock()
        app.send_notification.side_effect = lambda _id, n: (sent.append(n), loop.quit())
        app.notify.side_effect = lambda *a, **k: (sent.append(a), loop.quit())
        session = shots.ShotSession.__new__(shots.ShotSession)
        session.app, session.busy, session.card, session.text_mode = app, True, False, True
        session._done = lambda: None
        copied = []
        with mock.patch.object(shots, "WORK_DIR", tmp), \
                mock.patch("framewise.ocr.copy_text", side_effect=lambda t: copied.append(t) or True):
            session._read_text(still, ("area", (0, 0, 600, 400)), 1.0, (0, 0, 600, 400))
            GLib.timeout_add_seconds(20, loop.quit)
            loop.run()
        self.assertEqual(copied, ["https://example.com/hello"])
        self.assertEqual(len(sent), 1)
        self.assertFalse((tmp / "text.png").exists())
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
