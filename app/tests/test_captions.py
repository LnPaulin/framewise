import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gi.repository import GLib

from framewise import captions, export, gpu
from framewise import edit_model as em


def words(*spec):
    """("Hello", 0.0, 0.4), ... -> speech words."""
    return [{"word": w, "start": a, "end": b} for w, a, b in spec]


class BuildTests(unittest.TestCase):
    def test_lines_break_at_sentences_pauses_and_length(self):
        w = words(("hello", 0.0, 0.3), ("everyone,", 0.35, 0.8), ("um", 0.9, 1.1), ("today", 1.2, 1.5),
                  ("we", 1.55, 1.7), ("record", 1.75, 2.1), ("the", 2.15, 2.2), ("screen.", 2.25, 2.7),
                  ("Then", 4.0, 4.3), ("we", 4.35, 4.45), ("edit.", 4.5, 4.9))
        items = captions.build(w)
        # 42 characters max: the first sentence is 43 long, so it is split
        # (the rest of the sentence keeps its small letter).
        texts = [c["text"] for c in items]
        self.assertEqual(texts, ["Hello everyone, today we record the", "screen.", "Then we edit."])
        self.assertTrue(all(len(t) <= captions.MAX_CHARS for t in texts), texts)
        self.assertNotIn("um", " ".join(texts).lower().split())
        self.assertEqual(texts[-1], "Then we edit.")
        self.assertAlmostEqual(items[-1]["start"], 4.0)
        for a, b in zip(items, items[1:]):
            self.assertLessEqual(a["end"], b["start"])

    def test_pause_breaks(self):
        items = captions.build(words(("one", 0, 0.3), ("two", 0.35, 0.6), ("three", 2.0, 2.3)))
        self.assertEqual([c["text"] for c in items], ["One two", "three"])   # no full stop: same sentence
        self.assertGreaterEqual(items[1]["end"] - items[1]["start"], captions.MIN_SHOW - 1e-6)

    def test_nothing(self):
        self.assertEqual(captions.build([]), [])
        self.assertEqual(captions.build(words(("um", 0, 0.4), ("uh", 1, 1.2))), [])


class FileTests(unittest.TestCase):
    def doc(self):
        d = em.new_document(20.0)
        d["captions"]["items"] = [
            {"id": "a", "start": 1.0, "end": 3.0, "text": "First line"},
            {"id": "b", "start": 5.0, "end": 7.0, "text": "Cut away"},
            {"id": "c", "start": 10.0, "end": 12.0, "text": "After {the} cut \\ here"},
        ]
        d["captions"]["on"] = True
        return em.normalize(d, 20.0)

    def test_srt_follows_cuts(self):
        d = self.doc()
        d["cuts"] = [[4.0, 8.0]]
        d = em.normalize(d, 20.0)
        srt = captions.to_srt(d)
        self.assertIn("1\n00:00:01,000 --> 00:00:03,000\nFirst line", srt)
        self.assertNotIn("Cut away", srt)
        self.assertIn("2\n00:00:06,000 --> 00:00:08,000\nAfter {the} cut \\ here", srt)

    def test_srt_follows_speed(self):
        d = self.doc()
        d["export"]["speed"] = 2.0
        srt = captions.to_srt(em.normalize(d, 20.0))
        self.assertIn("00:00:00,500 --> 00:00:01,500", srt)

    def test_ass_is_safe(self):
        ass = captions.to_ass(self.doc(), 1920, 1080)
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("Dialogue: 0,0:00:10.00,0:00:12.00,Caption,,0,0,0,,After (the) cut \\\\ here", ass)

    def test_edits(self):
        d = self.doc()
        self.assertTrue(em.needs_compose(d))
        self.assertFalse(em.is_unedited(d, 20.0))
        d["captions"]["on"] = False
        self.assertFalse(em.has_captions(d))
        d["captions"]["items"][0]["text"] = "   "
        d = em.normalize(d, 20.0)
        self.assertEqual(len(d["captions"]["items"]), 2)      # an emptied line goes
        self.assertEqual(captions.at(d, 1.5), None)            # off: nothing shown


class BurnTests(unittest.TestCase):
    """Captions are drawn into the saved video."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.take = self.dir / "take.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x305080:s=640x360:r=30:d=4",
                        "-f", "lavfi", "-i", "sine=d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "flac", str(self.take)], check=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _save(self, with_captions, hw):
        d = em.new_document(4.0)
        d["zoom"]["enabled"] = False
        d["captions"] = {"on": with_captions, "size": "large", "position": "bottom",
                         "items": [{"id": "x", "start": 1.0, "end": 3.0, "text": "Hello from Framewise"}]}
        d = em.normalize(d, 4.0)
        loop, res = GLib.MainLoop(), {}
        dest = self.dir / f"out-{with_captions}-{hw}.mp4"
        import unittest.mock as mock
        with mock.patch.object(gpu, "ffmpeg_vaapi", return_value=hw):
            export.save_async(self.take, dest, None, lambda ok, m: (res.update(ok=ok, m=m), loop.quit()),
                              doc=d, take_info={"fps": 30, "microphone": True, "system_audio": False}, duration=4.0)
            GLib.timeout_add_seconds(60, loop.quit)
            loop.run()
        self.assertTrue(res.get("ok"), res)
        return dest

    def frame(self, path, t):
        import numpy as np
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(360, 640)

    def _check(self, hw):
        out = self._save(True, hw)
        during, after = self.frame(out, 2.0), self.frame(out, 3.5)
        # White letters in the bottom part while the caption is on, none after.
        self.assertGreater((during[250:350] > 220).sum(), 300)
        self.assertLess((after[250:350] > 220).sum(), 5)
        self.assertLess((during[:200] > 220).sum(), 5)             # nothing at the top

    def test_burned_in_software(self):
        self._check(False)

    @unittest.skipUnless(gpu.ffmpeg_vaapi(), "no VA-API")
    def test_burned_on_gpu(self):
        self._check(True)

    def test_off_means_none(self):
        out = self._save(False, False)
        self.assertLess((self.frame(out, 2.0)[250:350] > 220).sum(), 5)


if __name__ == "__main__":
    unittest.main()
