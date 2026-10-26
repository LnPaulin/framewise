"""Finds idle moments in a recording: the stretches where nothing happens.

A moment is idle only when ALL of these hold:
  - nothing changes on screen (text appearing, scrolling, a page loading...),
  - nobody speaks on the microphone and the system sound is quiet,
  - no typing, no clicks and the pointer doesn't move.

Screen changes are measured on a small greyscale copy of the recording, a
few frames a second. Parts that change nearly all the time (the camera
bubble, a looping animation) are left out, or nothing would ever count as
idle; so are changes of a few pixels (a blinking text cursor, the clock).
Idle stretches longer than MIN_IDLE become sped-up parts, keeping a short
beat at the start (so the last change can be seen) and at the end.
"""

import json
import subprocess
import threading
from pathlib import Path

import numpy as np
from gi.repository import GLib

FPS = 8                 # frames a second looked at
WIDTH = 384             # width of the copy compared (height keeps the shape)
DIFF = 10               # grey levels a pixel must change to count
MIN_PIXELS = 4          # changed pixels (outside the masked parts) that make a change
BLOCK = 8               # the mask's block size, in pixels of the copy
ALWAYS_MOVING = 0.5     # blocks changing in more than this share of frames are left out
MOVE = 0.004            # pointer movement (fraction of the picture) that counts
CLICK_PAD = 0.5         # seconds around a click that count as activity
TYPING_PAD = 0.7        # ... around a key press
ACTIVITY_PAD = 0.25     # every activity is widened by this much
SOUND_FLOOR_DB = -50.0  # quieter than this is never "sound"

MIN_IDLE = 1.5          # shortest idle stretch to speed up, in seconds
LEAD = 0.5              # kept at normal speed at the start of a stretch
TAIL = 0.3              # ... and at its end
MIN_PART = 0.6          # shortest sped-up part worth making


# ---- the signals, as one True/False per analysed frame ---------------------------------

def screen_activity(take, crop_px=None, fps=FPS):
    """Whether the picture changes, per frame (index i covers [i/fps, (i+1)/fps)).
    Frames are read as a stream: only per-block change counts are kept."""
    vf = (f"crop={crop_px[2]}:{crop_px[3]}:{crop_px[0]}:{crop_px[1]}," if crop_px else "")
    vf += f"fps={fps},scale={WIDTH}:-2,format=gray"
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-i", str(take), "-frames:v", "1",
                            "-vf", vf, "-f", "rawvideo", "-"], capture_output=True, timeout=60).stdout
    height = len(probe) // WIDTH if probe else 0
    if not height:
        return np.zeros(0, bool)
    proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-v", "error", "-i", str(take), "-vf", vf,
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = WIDTH * height
    counts, prev = [], None
    try:
        while True:
            raw = proc.stdout.read(size)
            if len(raw) < size:
                break
            cur = np.frombuffer(raw, np.uint8).reshape(height, WIDTH)
            counts.append(block_counts(prev, cur))
            prev = cur
    finally:
        proc.stdout.close()
        proc.wait()
    return activity_from_counts(np.array(counts))


def block_counts(prev, cur):
    """Changed pixels per BLOCK x BLOCK block between two frames (zeros for the first)."""
    h, w = cur.shape
    bh, bw = h // BLOCK, w // BLOCK
    if prev is None:
        return np.zeros((bh, bw), np.uint16)
    diff = np.abs(cur[: bh * BLOCK, : bw * BLOCK].astype(np.int16)
                  - prev[: bh * BLOCK, : bw * BLOCK].astype(np.int16)) > DIFF
    return diff.reshape(bh, BLOCK, bw, BLOCK).sum(axis=(1, 3)).astype(np.uint16)


def activity_from_counts(counts):
    """Per frame, whether enough changed outside the always-moving blocks.
    counts: frames x blocks-high x blocks-wide changed-pixel counts."""
    if len(counts) < 2:
        return np.zeros(len(counts), bool)
    always = (counts[1:] > 0).mean(axis=0) > ALWAYS_MOVING     # the camera bubble, a looping animation
    changed = (counts * ~always).sum(axis=(1, 2))
    active = changed >= MIN_PIXELS
    active[0] = False
    return active


def changes(frames):
    """Per frame, whether it differs from the one before (frames: n x h x w greyscale)."""
    counts = [block_counts(frames[i - 1] if i else None, frames[i]) for i in range(len(frames))]
    return activity_from_counts(np.array(counts))


def input_activity(pointer, duration, fps=FPS):
    """Clicks, typing and pointer movement from pointer.json (the recording's own clock)."""
    n = int(np.ceil(duration * fps))
    active = np.zeros(n, bool)
    if not pointer:
        return active

    def mark(a, b):
        i, j = max(0, int(a * fps)), min(n, int(np.ceil(b * fps)))
        if j > i:
            active[i:j] = True

    for t in pointer.get("clicks") or []:
        mark(t - CLICK_PAD, t + CLICK_PAD)
    for t, *_xy in pointer.get("typing") or []:
        mark(t - TYPING_PAD, t + TYPING_PAD)
    # How far the pointer travels within each analysed frame (slow, deliberate
    # moves count too; a pointer resting doesn't).
    travel = np.zeros(n)
    samples = pointer.get("samples") or []
    for (t0, x0, y0, *_), (_t1, x1, y1, *_) in zip(samples, samples[1:]):
        i = int(t0 * fps)
        if 0 <= i < n:
            travel[i] += abs(x1 - x0) + abs(y1 - y0)
    return active | (travel > MOVE)


def sound_activity(analysis, duration, fps=FPS):
    """Speech or other sound on one track (from silence.Analysis), per frame."""
    from . import silence
    n = int(np.ceil(duration * fps))
    if analysis is None:
        return np.zeros(n, bool)
    loud = analysis.db > max(analysis.threshold("medium"), SOUND_FLOOR_DB)
    per = 1.0 / fps / silence.WINDOW            # loudness windows per analysed frame
    out = np.zeros(n, bool)
    for i in range(n):
        a, b = int(i * per), int((i + 1) * per)
        out[i] = bool(loud[a:b].any()) if b > a else False
    return out


# ---- combining them ---------------------------------------------------------------------

def idle_parts(active, fps=FPS, within=None, min_idle=MIN_IDLE, lead=LEAD, tail=TAIL):
    """The parts to speed up, as [(start, end)] in seconds.

    active: True/False per frame (any signal). Each activity is widened a
    little first; idle stretches of at least min_idle become parts, minus a
    beat at both ends. within: (start, end) to stay inside (the trim)."""
    active = np.asarray(active, bool)
    pad = int(round(ACTIVITY_PAD * fps))
    if pad and active.size:
        widened = active.copy()
        for k in range(1, pad + 1):
            widened[k:] |= active[:-k]
            widened[:-k] |= active[k:]
        active = widened
    parts, i, n = [], 0, len(active)
    while i < n:
        if active[i]:
            i += 1
            continue
        j = i
        while j < n and not active[j]:
            j += 1
        a, b = i / fps, j / fps
        if within:
            a, b = max(a, within[0]), min(b, within[1])
        if b - a >= min_idle:
            a2, b2 = a + lead, b - tail
            if b2 - a2 >= MIN_PART:
                parts.append((round(a2, 3), round(b2, 3)))
        i = j
    return parts


def combine(*signals):
    """Activity from several signals (any of them), trimmed to the shortest."""
    signals = [np.asarray(s, bool) for s in signals if s is not None and len(s)]
    if not signals:
        return np.zeros(0, bool)
    n = max(len(s) for s in signals)
    out = np.zeros(n, bool)
    for s in signals:
        out[: len(s)] |= s
    return out


# ---- the whole analysis, in the background, cached --------------------------------------

def analyse_async(take, take_dir, duration, crop_px, tracks, on_done):
    """Measures every signal in the background; on_done(activity array or None).

    The result is cached in the take folder (idle.npz), so opening the panel
    again, or changing the speed, is instant."""
    cache = Path(take_dir) / "idle.npz"

    def work():
        result = None
        try:
            if cache.exists() and cache.stat().st_mtime >= Path(take).stat().st_mtime:
                result = np.load(cache)["active"]
            else:
                from . import silence
                screen = screen_activity(take, crop_px)
                pointer = None
                pointer_file = Path(take_dir) / "pointer.json"
                if pointer_file.exists():
                    pointer = json.loads(pointer_file.read_text())
                inputs = input_activity(pointer, duration)
                sounds = [sound_activity(silence.analyse(take, i, len(tracks)), duration)
                          for i in range(len(tracks))]
                result = combine(screen, inputs, *sounds)
                np.savez_compressed(cache, active=result)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            result = None
        GLib.idle_add(on_done, result)

    threading.Thread(target=work, daemon=True).start()
