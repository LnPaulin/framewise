import unittest

import numpy as np

from framewise import silence


def signal(pattern):
    """pattern: [(seconds, loud?)] -> loudness per 20 ms window."""
    parts = []
    for seconds, loud in pattern:
        n = int(round(seconds / silence.WINDOW))
        parts.append(np.full(n, -20.0 if loud else -60.0))
    return np.concatenate(parts)


class SilenceTests(unittest.TestCase):
    def test_finds_pause_between_words(self):
        db = signal([(2, True), (1.5, False), (2, True)])
        found = silence.find_silences(db, -40, min_pause=0.8, keep=0.2)
        self.assertEqual(len(found), 1)
        a, b = found[0]
        self.assertAlmostEqual(a, 2.2, places=2)
        self.assertAlmostEqual(b, 3.3, places=2)

    def test_short_pause_kept(self):
        db = signal([(2, True), (0.5, False), (2, True)])
        self.assertEqual(silence.find_silences(db, -40, min_pause=0.8), [])

    def test_edges_cut_fully(self):
        db = signal([(1.0, False), (2, True), (1.0, False)])
        found = silence.find_silences(db, -40, min_pause=0.8, keep=0.2)
        self.assertEqual(found[0][0], 0.0)
        self.assertAlmostEqual(found[-1][1], 4.0, places=2)

    def test_threshold_adapts(self):
        noisy = silence.Analysis(signal([(3, True), (3, False)]) + 15)
        quiet = silence.Analysis(signal([(3, True), (3, False)]))
        self.assertGreater(noisy.threshold(), quiet.threshold())
        self.assertEqual(len(noisy.silences()), 1)

    def test_within_limits(self):
        db = signal([(1, True), (3, False), (1, True)])
        found = silence.find_silences(db, -40, within=(2.0, 5.0))
        self.assertGreaterEqual(found[0][0], 2.0)



class FillerTests(unittest.TestCase):
    def test_filler_words(self):
        from framewise import speech
        for w in ("um", "Um,", "Ummm", "uh", "Uhh.", "hmm", "Hmm...", "erm", "mm", "euh", "ah"):
            self.assertTrue(speech.is_filler(w), w)
        for w in ("I", "a", "um-brella", "hum", "summer", "ok", "mom", "her", "eh?ok"):
            self.assertFalse(speech.is_filler(w), w)

    def test_filler_cuts_stay_between_words(self):
        from framewise import speech
        words = [{"word": "So", "start": 0.0, "end": 0.3},
                 {"word": "um,", "start": 0.32, "end": 0.7},
                 {"word": "yes", "start": 0.72, "end": 1.0}]
        cuts = speech.filler_cuts(words)
        self.assertEqual(len(cuts), 1)
        a, b = cuts[0]
        self.assertGreaterEqual(a, 0.3)
        self.assertLessEqual(b, 0.72)

    def test_repeated_fillers_are_one_cut(self):
        from framewise import speech
        words = [{"word": "But,", "start": 0.0, "end": 0.4},
                 {"word": "erm,", "start": 0.9, "end": 1.0},
                 {"word": "erm,", "start": 1.5, "end": 1.5},
                 {"word": "well", "start": 2.0, "end": 2.3}]
        self.assertEqual(speech.filler_cuts(words), [(0.48, 1.92)])

    def test_long_gap_only_cuts_the_voice(self):
        import numpy as np
        from framewise import speech
        words = [{"word": "So", "start": 0.0, "end": 0.3},
                 {"word": "um", "start": 0.5, "end": 0.6},
                 {"word": "yes", "start": 3.0, "end": 3.3}]
        db = np.full(200, -60.0)
        db[20:40] = -20.0  # the "um" is heard from 0.4 to 0.8 s
        (a, b), = speech.filler_cuts(words, db, -40)
        self.assertAlmostEqual(a, 0.38, places=2)
        self.assertAlmostEqual(b, 0.9, places=2)


if __name__ == "__main__":
    unittest.main()
