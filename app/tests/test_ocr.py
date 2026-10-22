import unittest

from framewise import ocr

HEAD = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def word(block, par, line, left, width, text, conf=95):
    return f"5\t1\t{block}\t{par}\t{line}\t1\t{left}\t0\t{width}\t10\t{conf}\t{text}\n"


class LayoutTests(unittest.TestCase):
    def test_indent_and_blocks(self):
        tsv = HEAD + (word(1, 1, 1, 0, 30, "def") + word(1, 1, 1, 40, 40, "f():")
                      + word(1, 2, 1, 40, 60, "return") + word(2, 1, 1, 0, 50, "Hello"))
        self.assertEqual(ocr.layout(tsv), "def f():\n    return\n\nHello")

    def test_drops_low_confidence_lines(self):
        tsv = HEAD + word(1, 1, 1, 0, 50, "Hello") + word(1, 1, 2, 0, 10, "a", conf=5)
        self.assertEqual(ocr.layout(tsv), "Hello")

    def test_empty(self):
        self.assertEqual(ocr.layout(HEAD), "")

    def test_languages(self):
        self.assertEqual(ocr.pick_languages(["fra", "eng"]), ["eng", "fra"])
        self.assertEqual(ocr.pick_languages(["deu"]), ["deu"])
        many = ["eng", "fra", "deu", "spa", "ita", "por"]
        self.assertIn("eng", ocr.pick_languages(many))
        self.assertLess(len(ocr.pick_languages(many)), len(many))


if __name__ == "__main__":
    unittest.main()
