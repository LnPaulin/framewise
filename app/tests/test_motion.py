import unittest

from framewise import motion


class MotionTests(unittest.TestCase):
    def test_one_zoom_per_click_with_lead_in(self):
        segs = motion.segments_from_clicks([2.0, 10.0], duration=20)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0]["start"], 1.7)
        self.assertAlmostEqual(segs[0]["end"], 4.5)

    def test_burst_of_clicks_is_one_zoom(self):
        segs = motion.segments_from_clicks([2.0, 3.0, 5.5], duration=20)
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(segs[0]["end"], 8.0)

    def test_stop_click_ignored(self):
        self.assertEqual(motion.segments_from_clicks([9.5], duration=10), [])

    def test_progress_eases(self):
        segs = [{"start": 1.0, "end": 3.0, "focus": None}]
        self.assertEqual(motion.progress(0.5, segs)[0], 0.0)
        mid = motion.progress(1.21, segs)[0]
        self.assertTrue(0.3 < mid < 0.7)
        self.assertEqual(motion.progress(2.0, segs)[0], 1.0)
        self.assertEqual(motion.progress(4.0, segs)[0], 0.0)

    def test_viewport_stays_inside_and_zooms(self):
        segs = [{"start": 1.0, "end": 3.0, "focus": None}]
        samples = [(0.0, 0.95, 0.95, 0), (5.0, 0.95, 0.95, 0)]  # pointer in a corner
        views = motion.viewports(segs, 2.0, samples, duration=5, fps=30)
        x, y, w, h = views[60]  # t = 2 s, fully zoomed
        self.assertAlmostEqual(w, 0.5, places=3)
        for v in views:
            self.assertGreaterEqual(v[0], 0)
            self.assertLessEqual(v[0] + v[2], 1.0001)
        self.assertEqual(views[0], (0.0, 0.0, 1.0, 1.0))
        self.assertGreater(x, 0.3)  # leaning toward the pointer's corner

    def test_hand_aimed_focus(self):
        segs = [{"start": 0.0, "end": 4.0, "focus": [0.25, 0.5]}]
        views = motion.viewports(segs, 2.0, [], duration=4, fps=30)
        x, y, w, h = views[90]
        self.assertAlmostEqual(x + w / 2, 0.25, places=2)  # centred on the spot

    def test_normalize_no_overlap(self):
        segs = motion.normalize_segments([{"start": 1, "end": 4, "focus": None},
                                          {"start": 3, "end": 6, "focus": None}], 10)
        self.assertEqual(segs[1]["start"], 4)


if __name__ == "__main__":
    unittest.main()


class TypingTests(unittest.TestCase):
    def test_typing_makes_one_zoom(self):
        typing = [(5.0 + 0.2 * k, 0.8, 0.3) for k in range(20)]       # 4 s of typing
        segs = motion.segments_from_activity([], typing, 30.0)
        self.assertEqual(len(segs), 1)
        self.assertLess(segs[0]["start"], 5.0)
        self.assertGreater(segs[0]["end"], 8.8)

    def test_view_looks_at_the_text_while_typing(self):
        typing = [(5.0 + 0.2 * k, 0.8, 0.3) for k in range(20)]
        samples = [(t / 10, 0.1, 0.9, 0, 1) for t in range(300)]        # pointer far away
        segs = motion.segments_from_activity([], typing, 30.0)
        views = motion.viewports(segs, 2.0, samples, 30.0, fps=30, typing=typing)
        x, y, w, h = views[int(8.0 * 30)]                                # while typing
        self.assertTrue(x <= 0.8 <= x + w and y <= 0.3 <= y + h, (x, y, w, h))
        x, y, w, h = views[int(12.0 * 30)]                               # after it, zoomed out
        self.assertEqual((x, y, w, h), (0.0, 0.0, 1.0, 1.0))

    def test_no_jitter_per_character(self):
        typing = [(1.0 + 0.1 * k, 0.40 + 0.004 * k, 0.5) for k in range(20)]
        spots = [s for s in motion.typing_focus(typing, 120, 30, 2.0) if s]
        self.assertLessEqual(len(set(spots)), 2)
