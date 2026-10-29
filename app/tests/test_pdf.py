import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from framewise import pdf


def make_pdf(path, pages, label="Page", photo=False):
    """A PDF with numbered pages (and a noisy picture on each, to compress)."""
    import cairo
    import numpy as np
    s = cairo.PDFSurface(str(path), 595, 842)
    rng = np.random.default_rng(len(str(path)))
    for n in range(1, pages + 1):
        cr = cairo.Context(s)
        cr.set_font_size(40)
        cr.move_to(60, 100)
        cr.show_text(f"{label} {n}")
        if photo:
            data = rng.integers(0, 255, (1200, 1200, 4), dtype=np.uint8)
            img = cairo.ImageSurface.create_for_data(memoryview(data), cairo.FORMAT_RGB24, 1200, 1200)
            cr.save()
            cr.translate(60, 150)
            cr.scale(0.4, 0.4)
            cr.set_source_surface(img, 0, 0)
            cr.paint()
            cr.restore()
        s.show_page()
    s.finish()
    return path


def text_of(path):
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True).stdout


@unittest.skipUnless(pdf.available(), "Poppler tools or Ghostscript missing")
class PdfTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.a = make_pdf(self.dir / "Report.pdf", 3, "Report")
        self.b = make_pdf(self.dir / "Annex.pdf", 2, "Annex")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_merge_in_order_with_a_picture(self):
        import cv2
        import numpy as np
        pic = self.dir / "scan.jpg"
        cv2.imwrite(str(pic), np.full((400, 300, 3), 200, np.uint8))
        out = pdf.merge([self.b, pic, self.a], pdf.output_path(self.b, "merged"))
        self.assertEqual(out.name, "Annex (merged).pdf")
        self.assertEqual(pdf.page_count(out), 2 + 1 + 3)
        t = text_of(out)
        self.assertLess(t.index("Annex 2"), t.index("Report 1"))

    def test_pick_pages(self):
        out = pdf.pick_pages(self.a, pdf.parse_pages("3, 1-2", 3), self.dir / "picked.pdf")
        self.assertEqual(pdf.page_count(out), 3)
        t = text_of(out)
        self.assertLess(t.index("Report 3"), t.index("Report 1"))

    def test_parse_pages(self):
        self.assertEqual(pdf.parse_pages("1-3, 5", 9), [1, 2, 3, 5])
        self.assertEqual(pdf.parse_pages("8-", 9), [8, 9])
        self.assertEqual(pdf.parse_pages("-2", 9), [1, 2])
        for bad in ("", "0", "4-2", "10", "a"):
            with self.assertRaises(pdf.PdfError):
                pdf.parse_pages(bad, 9)

    def test_split_all(self):
        folder = pdf.split_all(self.a, self.dir / "Report pages")
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, ["Report page 1.pdf", "Report page 2.pdf", "Report page 3.pdf"])

    def test_compress(self):
        big = make_pdf(self.dir / "Scans.pdf", 3, "Scan", photo=True)
        out, saved = pdf.compress(big, self.dir / "small.pdf", "smallest")
        self.assertGreater(saved, 0)
        self.assertLess(out.stat().st_size, big.stat().st_size)
        self.assertEqual(pdf.page_count(out), 3)
        self.assertIn("Scan 2", text_of(out))

    def test_compress_refuses_when_no_gain(self):
        with self.assertRaises(pdf.PdfError):
            pdf.compress(self.a, self.dir / "small.pdf")
        self.assertFalse((self.dir / "small.pdf").exists())

    def test_rotate(self):
        """The title (top left on the page) ends up where a clockwise turn puts it."""
        import cv2
        import numpy as np
        where = {}
        for degrees in (90, 180, 270):
            out = pdf.rotate(self.a, self.dir / f"rotated{degrees}.pdf", degrees)
            self.assertEqual(pdf.page_count(out), 3)
            subprocess.run(["pdftoppm", "-png", "-r", "20", "-f", "1", "-l", "1", str(out),
                            str(self.dir / f"r{degrees}")], check=True)
            img = cv2.imread(str(next(self.dir.glob(f"r{degrees}*.png"))), cv2.IMREAD_GRAYSCALE)
            ys, xs = np.nonzero(img < 128)
            h, w = img.shape
            where[degrees] = ("top" if ys.mean() < h / 2 else "bottom", "left" if xs.mean() < w / 2 else "right",
                              "landscape" if w > h else "portrait")
        self.assertEqual(where[90], ("top", "right", "landscape"))
        self.assertEqual(where[180], ("bottom", "right", "portrait"))
        self.assertEqual(where[270], ("bottom", "left", "landscape"))

    def test_images_to_pdf_and_back(self):
        import cv2
        import numpy as np
        pics = []
        for i, (w, h) in enumerate(((800, 600), (300, 900))):
            p = self.dir / f"photo{i}.jpg"
            cv2.imwrite(str(p), np.full((h, w, 3), 90 + i * 60, np.uint8))
            pics.append(p)
        out = pdf.images_to_pdf(pics, self.dir / "photos.pdf")
        self.assertEqual(pdf.page_count(out), 2)
        folder = pdf.to_images(out, self.dir / "back", dpi=96)
        pngs = sorted(folder.glob("*.png"))
        self.assertEqual(len(pngs), 2)
        first = cv2.imread(str(pngs[0]))
        self.assertAlmostEqual(first.shape[1], 800, delta=4)
        self.assertAlmostEqual(first.shape[0], 600, delta=4)

    def test_never_overwrites(self):
        a = pdf.output_path(self.a, "merged")
        a.write_bytes(b"x")
        self.assertEqual(pdf.output_path(self.a, "merged").name, "Report (merged) 2.pdf")

    def test_not_a_pdf(self):
        bad = self.dir / "fake.pdf"
        bad.write_text("hello")
        with self.assertRaises(pdf.PdfError):
            pdf.page_count(bad)


if __name__ == "__main__":
    unittest.main()
