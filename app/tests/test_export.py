import unittest

from framewise import export


class CropTests(unittest.TestCase):
    def test_even_and_inside(self):
        x, y, w, h = export.crop_pixels((0.1, 0.1, 0.3333, 0.5), (3440, 1440))
        for v in (x, y, w, h):
            self.assertEqual(v % 2, 0)
        self.assertLessEqual(x + w, 3440)
        self.assertLessEqual(y + h, 1440)

    def test_clamped_at_edge(self):
        x, y, w, h = export.crop_pixels((0.9, 0.9, 0.5, 0.5), (1000, 1000))
        self.assertEqual((x, y), (900, 900))
        self.assertEqual((w, h), (100, 100))

    def test_never_tiny(self):
        _, _, w, h = export.crop_pixels((0, 0, 0.001, 0.001), (3440, 1440))
        self.assertGreaterEqual(w, 32)
        self.assertGreaterEqual(h, 32)


class CommandTests(unittest.TestCase):
    def test_whole_screen_copies_video(self):
        cmd = export.build_command("t.mkv", "o.mp4", None, 2)
        self.assertIn("copy", cmd)
        self.assertNotIn("-hwaccel", cmd)
        self.assertTrue(any("amix=inputs=2" in c for c in cmd))

    def test_area_reencodes_on_gpu(self):
        cmd = export.build_command("t.mkv", "o.mp4", (10, 20, 300, 200), 1, hw=True)
        self.assertIn("h264_vaapi", cmd)
        self.assertTrue(any("crop=300:200:10:20" in c for c in cmd))
        # Square pixels, or players stretch the area back to the screen shape.
        self.assertTrue(any("setsar=1" in c for c in cmd))
        self.assertIn("0:a:0", cmd)

    def test_area_reencodes_in_software(self):
        # No graphics chip (NVIDIA driver, virtual machine): x264, same crop.
        cmd = export.build_command("t.mkv", "o.mp4", (10, 20, 300, 200), 1, hw=False)
        self.assertIn("libx264", cmd)
        self.assertNotIn("h264_vaapi", cmd)
        self.assertTrue(any("crop=300:200:10:20" in c and "setsar=1" in c for c in cmd))

    def test_no_audio(self):
        cmd = export.build_command("t.mkv", "o.mp4", None, 0)
        self.assertNotIn("aac", cmd)


if __name__ == "__main__":
    unittest.main()


class ExportOptionTests(unittest.TestCase):
    def doc(self, **export_opts):
        from framewise import edit_model as em
        d = em.new_document(10.0)
        d["export"].update(export_opts)
        return em.normalize(d, 10.0)

    def test_atempo_keeps_each_step_in_range(self):
        self.assertEqual(export.atempo_chain(1.0), "")
        self.assertEqual(export.atempo_chain(4.0), ",atempo=2,atempo=2")
        self.assertEqual(export.atempo_chain(0.25), ",atempo=0.5,atempo=0.5")
        self.assertEqual(export.atempo_chain(1.5), ",atempo=1.5")

    def test_speed_changes_video_and_sound(self):
        d = self.doc(speed=2.0)
        cmd = export.build_edit_command("t.mkv", "o.mp4", d, ["system", "mic"], fps=60,
                                        out={"speed": 2.0, "fps": 60})
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("setpts=N/(120*TB)", graph)
        self.assertEqual(graph.count("atempo=2"), 2)

    def test_speed_makes_it_edited(self):
        from framewise import edit_model as em
        self.assertTrue(em.is_unedited(self.doc(), 10.0))
        self.assertFalse(em.is_unedited(self.doc(speed=1.5), 10.0))
        self.assertAlmostEqual(em.output_duration(self.doc(speed=2.0)), 5.0)
        self.assertEqual(self.doc(speed=9)["export"]["speed"], 4.0)

    def test_small_is_half_size(self):
        out = export.output_settings(self.doc(quality="small"), (1920, 1080), None, 60, 10, True)
        self.assertEqual(out["size"], (960, 540))

    def test_fit_plan_budget(self):
        plan = export.fit_plan(60, (3440, 1440), 60, True, 10)
        self.assertLessEqual(plan["fps"], 30)
        total_bits = (plan["bitrate"] + 128_000) * 60
        self.assertLessEqual(total_bits / 8, 10_000_000)
        self.assertLessEqual(max(plan["size"]), 1920)
        self.assertIsNone(export.fit_plan(3600, (1920, 1080), 60, True, 10))  # an hour in 10 MB

    def test_bitrate_mode(self):
        cmd = export.build_edit_command("t.mkv", "o.mp4", self.doc(), [], fps=60, hw=True,
                                        out={"bitrate": 2_000_000, "fps": 30, "size": (1280, 720)})
        self.assertIn("VBR", cmd)
        self.assertIn("30", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("scale_vaapi=w=1280:h=720", graph)

    def test_bitrate_mode_in_software(self):
        cmd = export.build_edit_command("t.mkv", "o.mp4", self.doc(), [], fps=60, hw=False,
                                        out={"bitrate": 2_000_000, "fps": 30, "size": (1280, 720)})
        self.assertIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "2000000")
        self.assertIn("30", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("scale=1280:720", graph)

    def test_gif_size(self):
        self.assertEqual(export.gif_size((3440, 1440), "medium"), (600, 250))
        self.assertEqual(export.gif_size((400, 300), "large"), (400, 300))


class SpeedupExportTests(unittest.TestCase):
    """Idle moments: every Nth frame kept, the sound shortened to match."""

    def test_speedup_select_and_tempo(self):
        doc = em_doc(12.0, speedups=[[4.0, 8.0, 4]])
        cmd = export.build_edit_command("t.mkv", "o.mp4", doc, ["mic"], fps=60, hw=False)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("gte(t,4.0000)*lt(t,8.0000)*not(mod(n,4))", graph)
        self.assertIn("gte(t,0.0000)*lt(t,4.0000)+", graph)      # normal parts keep every frame
        self.assertIn("atrim=start=4.0000:end=8.0000,asetpts=PTS-STARTPTS,atempo=2,atempo=2", graph)


def em_doc(duration, speedups=()):
    from framewise import edit_model as em
    d = em.new_document(duration)
    d["speedups"] = [list(s) for s in speedups]
    return em.normalize(d, duration)


class SyncTests(unittest.TestCase):
    def test_shift_filters(self):
        from framewise import edit_model as em
        doc = em.normalize(em.new_document(10.0), 10.0)
        self.assertEqual(export._sync_shift(doc, "system"), "")
        doc["sync"]["system"] = 0.12
        self.assertIn("adelay=delays=120", export._sync_shift(doc, "system"))
        self.assertEqual(export._sync_shift(doc, "mic"), "")
        doc["sync"]["system"] = -0.05
        self.assertIn("atrim=start=0.0500", export._sync_shift(doc, "system"))

    def test_sync_is_an_edit_and_clamped(self):
        from framewise import edit_model as em
        doc = em.normalize(em.new_document(10.0), 10.0)
        self.assertTrue(em.is_unedited(doc, 10.0))
        doc["sync"]["system"] = 5.0
        doc = em.normalize(doc, 10.0)
        self.assertEqual(doc["sync"]["system"], 1.0)
        self.assertFalse(em.is_unedited(doc, 10.0))

    def test_tracks_start_from_zero(self):
        from framewise import edit_model as em
        doc = em.normalize(em.new_document(10.0), 10.0)
        cmd = export.build_edit_command("t.mkv", "o.mp4", doc, ["system", "mic"], hw=False)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(graph.count("aresample=async=1:first_pts=0"), 2)
