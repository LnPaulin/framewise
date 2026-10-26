"""Idle moments: what counts as activity, and which parts get sped up."""

import unittest

import numpy as np

from framewise import idle, silence

FPS = idle.FPS


def screen(n, h=120, w=192):
    rng = np.random.default_rng(0)
    frames = np.full((n, h, w), 40, np.uint8)
    # A "camera bubble" in the corner: changes in every frame (a face moving).
    frames[:, 80:120, 150:192] = rng.integers(0, 255, (n, 40, 42), dtype=np.uint8)
    # A blinking text cursor: 2 pixels, on and off.
    frames[::2, 10:12, 10] = 220
    return frames


class ScreenTests(unittest.TestCase):
    def test_camera_and_cursor_dont_count(self):
        self.assertFalse(idle.changes(screen(40)).any())

    def test_text_appearing_counts(self):
        frames = screen(40)
        frames[20:, 30:40, 20:90] = 230        # a line of text appears at frame 20
        active = idle.changes(frames)
        self.assertTrue(active[20])
        self.assertEqual(int(active.sum()), 1)  # only when it changes, not while it stays


class InputTests(unittest.TestCase):
    def test_clicks_typing_and_slow_movement(self):
        pointer = {"clicks": [2.0], "typing": [[6.0, 0.5, 0.5]],
                   # A slow move between 9 s and 10 s: 0.002 a sample (60 a second).
                   "samples": [[9 + k / 60, 0.3 + 0.002 * k, 0.4, 0, 1] for k in range(61)]}
        active = idle.input_activity(pointer, 12.0)
        at = lambda t: bool(active[int(t * FPS)])
        self.assertTrue(at(2.0) and at(2.4) and at(1.6))
        self.assertTrue(at(6.0) and at(6.6))
        self.assertTrue(at(9.5))
        self.assertFalse(at(4.0) or at(8.0) or at(11.0))

    def test_resting_pointer_is_idle(self):
        pointer = {"samples": [[k / 60, 0.5, 0.5, 0, 1] for k in range(600)]}
        self.assertFalse(idle.input_activity(pointer, 10.0).any())


class SoundTests(unittest.TestCase):
    def test_speech_over_a_still_screen(self):
        db = np.full(500, -70.0)               # 10 s of a quiet room (20 ms windows)
        db[150:250] = -20.0                    # talking from 3 s to 5 s
        active = idle.sound_activity(silence.Analysis(db), 10.0)
        self.assertTrue(active[int(4.0 * FPS)])
        self.assertFalse(active[int(7.0 * FPS)])

    def test_steady_hiss_is_not_sound(self):
        rng = np.random.default_rng(1)
        db = -62 + rng.normal(0, 2, 500)       # fan noise, no voice
        self.assertFalse(idle.sound_activity(silence.Analysis(db), 10.0).any())


class PartsTests(unittest.TestCase):
    def frames(self, seconds):
        return np.zeros(int(seconds * FPS), bool)

    def test_long_idle_sped_up_with_beats_kept(self):
        a = self.frames(12)
        a[: 2 * FPS] = True                    # busy until 2 s
        a[8 * FPS: 9 * FPS] = True             # busy 8-9 s
        a[10 * FPS: int(10.8 * FPS)] = True    # busy 10-10.8 s; 10.8-12 s is too short to speed up
        parts = idle.idle_parts(a)
        # Idle 2..8 s: the widened activity takes 0.25 s each side, then the beats.
        self.assertEqual(len(parts), 1)
        start, end = parts[0]
        self.assertAlmostEqual(start, 2 + idle.ACTIVITY_PAD + idle.LEAD, places=2)
        self.assertAlmostEqual(end, 8 - idle.ACTIVITY_PAD - idle.TAIL, places=2)

    def test_short_pauses_stay(self):
        a = np.ones(int(10 * FPS), bool)
        a[3 * FPS: int(4.2 * FPS)] = False     # a 1.2 s pause: natural, kept
        self.assertEqual(idle.idle_parts(a), [])

    def test_within_the_trim(self):
        a = self.frames(20)
        parts = idle.idle_parts(a, within=(5.0, 15.0))
        self.assertEqual(parts, [(5.0 + idle.LEAD, 15.0 - idle.TAIL)])

    def test_combine_any_signal(self):
        s = np.array([1, 0, 0, 0], bool)
        t = np.array([0, 0, 1], bool)
        self.assertEqual(idle.combine(s, t, None).tolist(), [True, False, True, False])


if __name__ == "__main__":
    unittest.main()
