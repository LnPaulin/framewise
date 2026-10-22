"""Finds the parts of a recording where nobody is speaking.

The microphone track (or all sound, if there is no microphone) is decoded
to a small mono signal and measured in 20 ms steps. The room's own noise
level and the speaking level are estimated from that, and the threshold sits
between them, so it adapts to quiet and noisy rooms alike. Quiet stretches
longer than the minimum pause become suggested cuts, shrunk a little at both
ends so words are never clipped.
"""

import subprocess
import threading

import numpy as np
from gi.repository import GLib

RATE = 8000
WINDOW = 0.02  # seconds per measurement

# How much of the gap between room noise and speech counts as silence.
SENSITIVITY = {"low": 0.22, "medium": 0.35, "high": 0.5}


class Analysis:
    """Loudness of the recording over time, ready for fast re-detection."""

    def __init__(self, db):
        # A short median smooths out clicks and taps, so a keyboard click
        # in the middle of a pause doesn't split it in two.
        self.db = _median(db, 5)        # loudness per 20 ms window, in dB
        finite = self.db[np.isfinite(self.db)]
        # Room noise from the quieter part; speech from the loudest moments.
        # (Speech can be a small share of a recording, so a high percentile.)
        self.floor = float(np.percentile(finite, 15)) if finite.size else -90.0
        self.speech = float(np.percentile(finite, 98)) if finite.size else -20.0

    def threshold(self, sensitivity="medium"):
        mix = SENSITIVITY.get(sensitivity, SENSITIVITY["medium"])
        return self.floor + mix * max(6.0, self.speech - self.floor)

    def silences(self, sensitivity="medium", min_pause=0.8, keep=0.2, within=None):
        return find_silences(self.db, self.threshold(sensitivity), min_pause, keep, within)


def _median(values, width):
    if len(values) < width:
        return values
    pad = width // 2
    padded = np.pad(values, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)
    return np.median(windows, axis=1)


def find_silences(db, threshold, min_pause=0.8, keep=0.2, within=None):
    """Quiet stretches as [(start, end)] in seconds.

    min_pause: shortest pause worth cutting. keep: seconds left in place at
    each end of a pause, so the cut never touches the words around it.
    within: (start, end) to limit the search to, e.g. the trimmed part.
    """
    quiet = db < threshold
    ranges, i, n = [], 0, len(quiet)
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        a, b = i * WINDOW, j * WINDOW
        # A pause at the very start or end has no words on that side to protect.
        a2 = a + (keep if i > 0 else 0.0)
        b2 = b - (keep if j < n else 0.0)
        if within:
            a2, b2 = max(a2, within[0]), min(b2, within[1])
        if b - a >= min_pause and b2 - a2 >= 0.1:
            ranges.append((round(a2, 3), round(b2, 3)))
        i = j
    return ranges


def analyse_async(take, track_index, on_done, track_count=2):
    """Measures the recording in the background. on_done(Analysis or None).

    track_index: which audio track to listen to, or None to mix them all.
    """
    # Each track from zero (one may start late: see recorder system_delay).
    pad = "aresample=async=1:first_pts=0"
    if track_index is None:
        inputs = "".join(f"[0:a:{i}]{pad}[p{i}];" for i in range(track_count))
        mix = "".join(f"[p{i}]" for i in range(track_count))
        args = ["-filter_complex", f"{inputs}{mix}amix=inputs={track_count}:normalize=0[a]", "-map", "[a]"]
    else:
        args = ["-map", f"0:a:{track_index}", "-af", pad]
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(take), *args,
           "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]

    def work():
        result = None
        try:
            raw = subprocess.run(cmd, capture_output=True, timeout=600).stdout
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            step = int(RATE * WINDOW)
            count = len(samples) // step
            if count:
                frames = samples[: count * step].reshape(count, step)
                rms = np.sqrt(np.mean(frames ** 2, axis=1))
                db = 20 * np.log10(np.maximum(rms, 1e-6))
                result = Analysis(db)
        except (OSError, subprocess.SubprocessError, ValueError):
            result = None
        GLib.idle_add(on_done, result)

    threading.Thread(target=work, daemon=True).start()
