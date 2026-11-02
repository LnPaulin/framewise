import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from framewise import camera_keep as ck
from framewise import edit_model as em
from framewise import export, gpu


class TrackTests(unittest.TestCase):
    BOX = ((100, 50, 200, 200), (100.0, 100.0, 100.0, 100.0))

    def test_place_counts_when_seen_twice(self):
        t = ck.Track()
        t.add(10.0, self.BOX)
        self.assertEqual(t.spots, [])
        t.add(10.5, self.BOX)
        t.add(11.0, self.BOX)
        self.assertEqual(len(t.spots), 1)
        self.assertEqual(t.spots[0][:2], [10.5, None])

    def test_moving_bubble_ends_where_last_seen(self):
        t = ck.Track()
        for i in range(4):
            t.add(10.0 + i * 0.5, self.BOX)
        moved = ((400, 50, 200, 200), self.BOX[1])
        t.add(12.0, ((300, 50, 200, 200), self.BOX[1]))      # being dragged
        t.add(12.5, moved)
        t.add(13.0, moved)
        self.assertEqual(len(t.spots), 2)
        self.assertEqual(t.spots[0][1], 11.5)                # last time it was there
        self.assertEqual(t.spots[1][0], 13.0)

    def test_hidden_bubble_ends_spot(self):
        t = ck.Track()
        t.add(1.0, self.BOX)
        t.add(1.5, self.BOX)
        t.add(2.0, None)
        self.assertEqual(t.spots[0][1], 1.5)

    def test_info_on_recording_clock_with_pause(self):
        t = ck.Track()
        t.add(100.0, self.BOX)
        t.add(100.5, self.BOX)
        # Recorded 100..102, paused, then 105.. (2 s already in the video).
        runs = [[100.0, 102.0, 0.0], [105.0, None, 2.0]]
        info = t.info(runs, (0, 0, 1000, 500), 106.0)
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["start"], 0.5)
        self.assertEqual(info[0]["end"], 3.0)
        self.assertEqual(info[0]["rect"], [0.1, 0.1, 0.2, 0.4])
        self.assertEqual(info[0]["radii"], [0.5, 0.5, 0.5, 0.5])

    def test_screen_box(self):
        box = ck.screen_box((1000, 600, 300, 300), ((14.0, 14.0, 272.0, 272.0), (136, 136, 136, 136)))
        self.assertEqual(box, ((1014, 614, 272, 272), (136, 136, 136, 136)))
        self.assertIsNone(ck.screen_box(None, ((0, 0, 1, 1), (0, 0, 0, 0))))


class AreaTests(unittest.TestCase):
    INFO = {"camera": [{"start": 0, "end": 5, "rect": [0.75, 0.5, 0.2, 0.4], "radii": [0, 0, 0, 0]}]}

    def test_whole_screen(self):
        (spot,) = ck.in_area(self.INFO)
        self.assertEqual(spot[2], (0.75, 0.5, 0.2, 0.4))

    def test_area_crop(self):
        (spot,) = ck.in_area(self.INFO, (0.5, 0.5, 0.5, 0.5))
        self.assertEqual(spot[2], (0.5, 0.0, 0.4, 0.8))

    def test_outside_area(self):
        self.assertEqual(ck.in_area(self.INFO, (0.0, 0.0, 0.5, 0.5)), [])
        self.assertEqual(ck.in_area({}), [])

    def test_circle_mask(self):
        m = ck.mask(100, 100, [50] * 4)
        self.assertEqual(m[50, 50], 255)
        self.assertEqual(m[0, 0], 0)
        self.assertEqual(m[50, 1], 255)
        self.assertEqual(ck.mask(10, 10, [0] * 4).min(), 255)


def _grey_at(path, t, x, y, w, h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                          "-vf", f"crop={w}:{h}:{x}:{y},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return sum(raw) / len(raw)


class ExportTests(unittest.TestCase):
    """A white square (the camera) at the bottom right of a grey picture; the
    video zooms into the top left. The square must stay where it was."""

    SIZE = (640, 360)
    CAM = (480, 220, 120, 120)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        x, y, w, h = self.CAM
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=gray:s={self.SIZE[0]}x{self.SIZE[1]}:r=30:d=2",
                        "-vf", f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.dir / "take.mkv")],
                       check=True)
        W, H = self.SIZE
        self.info = {"fps": 30, "camera": [{"start": 0.0, "end": 2.0,
                                            "rect": [x / W, y / H, w / W, h / H], "radii": [0, 0, 0, 0]}]}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _save(self, info):
        from gi.repository import GLib
        doc = em.new_document(2.0)
        doc["zoom"].update(enabled=True, amount=2.0,
                           segments=[{"start": 0.1, "end": 1.9, "focus": [0.1, 0.1]}])
        doc = em.normalize(doc, 2.0)
        loop, result = GLib.MainLoop(), {}

        def done(ok, message):
            result.update(ok=ok, message=message)
            loop.quit()
        dest = self.dir / "saved.mp4"
        export.save_async(self.dir / "take.mkv", dest, None, done, doc=doc, take_info=info, duration=2.0)
        GLib.timeout_add_seconds(60, loop.quit)
        loop.run()
        self.assertTrue(result.get("ok"), result)
        return dest

    def test_zoom_moves_camera_away_without_this(self):
        out = self._save({"fps": 30})
        self.assertLess(_grey_at(out, 1.0, 500, 240, 80, 80), 200)

    def test_camera_stays_put(self):
        out = self._save(self.info)
        self.assertGreater(_grey_at(out, 1.0, 500, 240, 80, 80), 230)
        # The rest is still zoomed: just outside the square is grey.
        self.assertLess(_grey_at(out, 1.0, 400, 240, 60, 80), 200)


class SoftwareExportTests(ExportTests):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(gpu, "ffmpeg_vaapi", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)


if __name__ == "__main__":
    unittest.main()
