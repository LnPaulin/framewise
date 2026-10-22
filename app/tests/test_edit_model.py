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


if __name__ == "__main__":
    unittest.main()
