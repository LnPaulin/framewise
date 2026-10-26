import unittest

from framewise import edit_model as em


class EditModelTests(unittest.TestCase):
    def doc(self, trim=(0, 10), cuts=()):
        d = em.new_document(10)
        d["trim"] = list(trim)
        d["cuts"] = [list(c) for c in cuts]
        return em.normalize(d, 10)

    def test_untouched(self):
        d = self.doc()
        self.assertEqual(em.kept_segments(d), [(0, 10)])
        self.assertTrue(em.is_unedited(d, 10))

    def test_trim_and_cut(self):
        d = self.doc(trim=(1, 9), cuts=[(3, 4), (6, 7)])
        self.assertEqual(em.kept_segments(d), [(1, 3), (4, 6), (7, 9)])
        self.assertAlmostEqual(em.finished_duration(d), 6)
        self.assertFalse(em.is_unedited(d, 10))

    def test_clock_mapping(self):
        d = self.doc(cuts=[(2, 5)])
        self.assertAlmostEqual(em.to_finished(d, 1), 1)
        self.assertIsNone(em.to_finished(d, 3))
        self.assertAlmostEqual(em.to_finished(d, 6), 3)
        self.assertAlmostEqual(em.to_recording(d, 3), 6)
        self.assertAlmostEqual(em.to_recording(d, 1.5), 1.5)
        self.assertAlmostEqual(em.snap_to_kept(d, 3), 5)
        self.assertEqual(em.cut_at(d, 2), 0)
        self.assertIsNone(em.cut_at(d, 5))  # the end of a cut is kept

    def test_cuts_merge_and_clamp(self):
        d = self.doc(trim=(1, 9), cuts=[(0, 2), (1.5, 3), (8.5, 12), (5, 5.05)])
        self.assertEqual(d["cuts"], [[0, 3], [8.5, 10]])
        self.assertEqual(em.kept_segments(d), [(3, 8.5)])

    def test_trimming_over_a_cut_keeps_it(self):
        d = self.doc(cuts=[(2, 3)])
        d["trim"] = [4, 10]
        d = em.normalize(d, 10)
        d["trim"] = [0, 10]
        d = em.normalize(d, 10)
        self.assertEqual(d["cuts"], [[2, 3]])

    def test_cut_outside_trim_ignored(self):
        d = self.doc(trim=(0, 5), cuts=[(7, 8)])
        self.assertEqual(em.kept_segments(d), [(0, 5)])
        d = self.doc(trim=(3, 10), cuts=[(1, 2)])
        self.assertEqual(em.kept_segments(d), [(3, 10)])

    def test_view_clock(self):
        d = self.doc(cuts=[(2, 5)])
        self.assertAlmostEqual(em.view_duration(d, 10), 7)
        self.assertAlmostEqual(em.to_view(d, 10, 6), 3)
        self.assertAlmostEqual(em.to_view(d, 10, 3), 2)   # inside the cut: its seam
        self.assertAlmostEqual(em.from_view(d, 10, 3), 6)
        self.assertEqual(em.seams(d, 10), [(2.0, 0)])

    def test_cannot_cut_everything(self):
        d = self.doc(cuts=[(0, 10)])
        self.assertGreater(em.finished_duration(d), 0)

    def test_trim_minimum(self):
        d = self.doc(trim=(5, 5))
        self.assertGreaterEqual(d["trim"][1] - d["trim"][0], em.MIN_TRIM - 1e-9)



class SpeedupTests(unittest.TestCase):
    """Idle moments played faster: pieces, durations and both clocks."""

    def doc(self, speedups, cuts=(), trim=(0.0, 20.0)):
        d = em.new_document(20.0)
        d["trim"] = list(trim)
        d["cuts"] = [list(c) for c in cuts]
        d["speedups"] = [list(s) for s in speedups]
        return em.normalize(d, 20.0)

    def test_pieces_split_around_speedups(self):
        d = self.doc([[5, 9, 4]])
        self.assertEqual(em.pieces(d), [(0.0, 5.0, 1), (5.0, 9.0, 4), (9.0, 20.0, 1)])
        # 16 s at normal speed + 4 s at 4x = 17 s.
        self.assertAlmostEqual(em.finished_duration(d), 17.0)

    def test_clocks_round_trip(self):
        d = self.doc([[5, 9, 4]])
        self.assertAlmostEqual(em.to_finished(d, 7.0), 5.5)      # halfway through the fast part
        self.assertAlmostEqual(em.to_finished(d, 10.0), 7.0)     # 5 + 1 + 1
        for t in (0.0, 4.9, 5.0, 6.3, 9.0, 15.0, 20.0):
            self.assertAlmostEqual(em.to_recording(d, em.to_finished(d, t)), t, places=6)

    def test_cut_wins_over_speedup(self):
        d = self.doc([[5, 9, 4]], cuts=[[6, 8]])
        self.assertEqual(em.pieces(d), [(0.0, 5.0, 1), (5.0, 6.0, 4), (8.0, 9.0, 4), (9.0, 20.0, 1)])
        self.assertIsNone(em.to_finished(d, 7.0))
        self.assertAlmostEqual(em.finished_duration(d), 16.5)

    def test_trim_limits_speedups(self):
        d = self.doc([[2, 8, 2]], trim=(4.0, 12.0))
        self.assertEqual(em.pieces(d), [(4.0, 8.0, 2), (8.0, 12.0, 1)])

    def test_normalize(self):
        d = self.doc([[9, 12, 4], [1, 3, 5], [2, 4, 4], [15, 15.1, 2], [10, 11, 4], [18, 25, 8]])
        # 5 -> nearest allowed rate (4); overlapping same-speed parts join;
        # too short ones go; the end is clamped to the recording.
        self.assertEqual(d["speedups"], [[1.0, 4.0, 4], [9.0, 12.0, 4], [18.0, 20.0, 8]])
        d = self.doc([[1, 5, 2], [3, 8, 4]])      # different speeds: the earlier keeps its part
        self.assertEqual(d["speedups"], [[1.0, 5.0, 2], [5.0, 8.0, 4]])

    def test_rate_and_index_lookup(self):
        d = self.doc([[5, 9, 4]])
        self.assertEqual(em.rate_at(d, 4.99), 1)
        self.assertEqual(em.rate_at(d, 5.0), 4)
        self.assertEqual(em.rate_at(d, 9.0), 1)
        self.assertEqual(em.speedup_at(d, 6), 0)
        self.assertIsNone(em.speedup_at(d, 10))

    def test_speedups_make_it_edited(self):
        self.assertTrue(em.is_unedited(self.doc([]), 20.0))
        self.assertFalse(em.is_unedited(self.doc([[5, 9, 4]]), 20.0))

if __name__ == "__main__":
    unittest.main()
