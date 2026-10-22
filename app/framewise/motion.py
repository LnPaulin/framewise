"""Zoom on click: where the picture leans in, and where it looks.

Everything here is plain maths
over a finished recording, so it can look ahead: a zoom starts slightly
BEFORE the click that causes it, which reads as deliberate rather than late.

Coordinates are fractions (0..1) of the recorded picture, top-left origin.
Times are recording times in seconds.
"""

import math

# Zoom timing (seconds)
LEAD_IN = 0.3          # starts this long before the click
HOLD = 2.5             # stays in this long after it
MIN_DURATION = 0.9
IGNORE_TAIL = 1.0      # the click that stops the recording never zooms
END_MARGIN = 0.8
RAMP_IN = 0.42
RAMP_OUT = 0.65
ADDED_LENGTH = 2.0     # a zoom added by hand

DEFAULT_AMOUNT = 1.8
AMOUNT_RANGE = (1.2, 3.0)

# Looking at what is typed
TYPING_HOLD = 1.5      # after the last key, the view stays on the text this long
TYPING_BOX_W = 0.45    # of what is visible: the view moves on only past this
TYPING_BOX_H = 0.3
TYPING_LEAD = 0.12     # of what is visible: room to the right of the cursor

# Following the pointer calmly
EDGE_BAND = 0.25
CLUSTER_W = 0.5        # of what is visible: sideways travel reads as normal
CLUSTER_H = 0.7        # vertical travel turns the stomach, so allow more
SPRING_STIFFNESS, SPRING_MASS, SPRING_DAMPING = 200.0, 2.25, 40.0


def smootherstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * x * (10 - 15 * x + 6 * x * x)


def segments_from_clicks(clicks, duration):
    """The first set of zooms, one per click or burst of clicks."""
    presses = sorted(t for t in clicks if t < duration - IGNORE_TAIL)
    merged = []
    for press in presses:
        start, end = max(0.001, press - LEAD_IN), press + HOLD
        if merged and start - merged[-1][1] <= HOLD:
            merged[-1][1] = max(merged[-1][1], end)  # a burst is one zoom that holds
        else:
            merged.append([start, end])
    limit = max(0.0, duration - END_MARGIN)
    out = []
    for start, end in merged:
        end = min(end, limit)
        if end - start >= MIN_DURATION:
            out.append({"start": round(start, 3), "end": round(end, 3), "focus": None})
    return out


def segments_from_activity(clicks, typing, duration):
    """Zooms from clicks and from typing: a burst of typing is one zoom that
    holds while you type (typing: [(t, x, y)] or plain times)."""
    times = [t[0] if isinstance(t, (list, tuple)) else t for t in (typing or [])]
    return segments_from_clicks(list(clicks or []) + times, duration)


def normalize_segments(segments, duration):
    """In order, inside the recording, never overlapping (a later one starts later)."""
    out = []
    for seg in sorted(segments, key=lambda s: s["start"]):
        seg = dict(seg)
        seg["start"] = max(0.0, min(seg["start"], duration))
        seg["end"] = max(0.0, min(seg["end"], duration))
        if out and seg["start"] < out[-1]["end"]:
            seg["start"] = out[-1]["end"]
        if seg["end"] - seg["start"] >= 0.4:
            out.append(seg)
    return out


def progress(t, segments):
    """0 when not zoomed, 1 while holding, eased at both ends. Also the segment."""
    for seg in segments:
        if seg["start"] - 0.001 <= t <= seg["end"] + RAMP_OUT:
            if t <= seg["start"] + RAMP_IN:
                return smootherstep((t - seg["start"]) / RAMP_IN), seg
            if t >= seg["end"]:
                return smootherstep(1 - (t - seg["end"]) / RAMP_OUT), seg
            return 1.0, seg
    return 0.0, None


def travel(focus):
    """Following the pointer: a calm edge band, so the view isn't always moving."""
    return min(1.0, max(0.0, (focus - EDGE_BAND) / (1 - 2 * EDGE_BAND)))


def travel_exact(focus, zoom):
    """A spot aimed at by hand: centre it as closely as the frame allows."""
    visible = 1 / max(1.0, zoom)
    span = 1 - visible
    if span <= 1e-4:
        return 0.5
    return min(1.0, max(0.0, (focus - visible / 2) / span))


def spot_view(fx, fy, amount):
    """What a zoom aimed at (fx, fy) shows when fully zoomed: (x, y, w, h)."""
    w = h = 1 / max(1.0, amount)
    return (travel_exact(fx, amount) * (1 - w), travel_exact(fy, amount) * (1 - h), w, h)


def focus_clusters(points, fps, zoom):
    """Groups pointer positions into boxes; the view aims at the box's centre.

    points: [(x, y)] one per frame. Returns [(time, (cx, cy))].
    """
    if not points:
        return []
    step = 1.0 / fps
    max_w, max_h = CLUSTER_W / max(1.0, zoom), CLUSTER_H / max(1.0, zoom)
    clusters = []
    x0 = x1 = points[0][0]
    y0 = y1 = points[0][1]
    started = 0.0
    for i, (x, y) in enumerate(points):
        nx0, nx1, ny0, ny1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
        if nx1 - nx0 <= max_w and ny1 - ny0 <= max_h:
            x0, x1, y0, y1 = nx0, nx1, ny0, ny1
            continue
        clusters.append((started, ((x0 + x1) / 2, (y0 + y1) / 2)))
        x0 = x1 = x
        y0 = y1 = y
        started = i * step
    clusters.append((started, ((x0 + x1) / 2, (y0 + y1) / 2)))
    return clusters


def springed(targets, fps):
    """A critically damped spring over the whole timeline (fixed 240 Hz steps)."""
    if len(targets) < 2:
        return list(targets)
    frame, sub = 1.0 / fps, 1.0 / 240
    value, velocity = targets[0], 0.0
    out = []
    for target in targets:
        elapsed = 0.0
        while elapsed < frame:
            dt = min(sub, frame - elapsed)
            accel = (SPRING_STIFFNESS * (target - value) - SPRING_DAMPING * velocity) / SPRING_MASS
            velocity += accel * dt
            value += velocity * dt
            elapsed += sub
        out.append(value)
    return out


def pointer_at_frames(samples, n_frames, fps):
    """Pointer position at each frame, from samples [(t, x, y, down)]."""
    if not samples:
        return [(0.5, 0.5)] * n_frames
    out, j = [], 0
    for i in range(n_frames):
        t = i / fps
        while j + 1 < len(samples) and samples[j + 1][0] <= t:
            j += 1
        a = samples[j]
        if j + 1 < len(samples) and samples[j + 1][0] > a[0] and t > a[0]:
            b = samples[j + 1]
            k = (t - a[0]) / (b[0] - a[0])
            out.append((a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k))
        else:
            out.append((a[1], a[2]))
    return out


def typing_focus(typing, n, fps, zoom):
    """Where the view should look at each frame while typing, else None.

    typing: [(t, x, y)] text-cursor places. The view moves only when the
    cursor leaves a box around the last spot (no jitter per character), and
    keeps a little room to the right of the cursor for what comes next.
    """
    out = [None] * n
    if not typing:
        return out
    typing = sorted(typing)
    box_w, box_h = TYPING_BOX_W / max(1.0, zoom), TYPING_BOX_H / max(1.0, zoom)
    lead = TYPING_LEAD / max(1.0, zoom)
    j, spot = -1, None
    for i in range(n):
        t = i / fps
        while j + 1 < len(typing) and typing[j + 1][0] <= t:
            j += 1
            _t, x, y = typing[j]
            if spot is None or abs(x + lead - spot[0]) > box_w / 2 or abs(y - spot[1]) > box_h / 2:
                spot = (x + lead, y)
        if j >= 0 and t - typing[j][0] <= TYPING_HOLD:
            out[i] = spot
        else:
            spot = None if j < 0 or t - typing[j][0] > TYPING_HOLD else spot
    return out


def viewports(segments, amount, samples, duration, fps=60, typing=None):
    """The visible part of the picture at every frame: [(x, y, w, h)] in 0..1.

    (0, 0, 1, 1) means not zoomed. Automatic zooms follow the pointer in
    calm steps, and look at the text while you type; a zoom with a "focus"
    looks at that spot.
    """
    n = max(1, int(math.ceil(duration * fps)))
    zoom_targets, tx_targets, ty_targets = [], [], []
    points = pointer_at_frames(samples, n, fps)
    clusters = focus_clusters(points, fps, amount)
    typed = typing_focus(typing, n, fps, amount)
    ci, current = 0, (clusters[0][1] if clusters else (0.5, 0.5))
    last_travel = (0.5, 0.5)
    for i in range(n):
        t = i / fps
        while ci < len(clusters) and clusters[ci][0] <= t:
            current = clusters[ci][1]
            ci += 1
        p, seg = progress(t, segments)
        zoom = 1 + (amount - 1) * p
        if seg is not None:
            if seg.get("focus"):
                fx, fy = seg["focus"]
                last_travel = (travel_exact(fx, amount), travel_exact(fy, amount))
            elif typed[i] is not None:
                fx, fy = typed[i]
                last_travel = (travel_exact(min(1.0, max(0.0, fx)), amount),
                               travel_exact(min(1.0, max(0.0, fy)), amount))
            else:
                last_travel = (travel(current[0]), travel(current[1]))
        zoom_targets.append(zoom)
        tx_targets.append(last_travel[0])
        ty_targets.append(last_travel[1])
    tx = springed(tx_targets, fps)
    ty = springed(ty_targets, fps)
    out = []
    for zoom, x, y in zip(zoom_targets, tx, ty):
        if zoom <= 1.0005:
            out.append((0.0, 0.0, 1.0, 1.0))
            continue
        w = h = 1 / zoom
        span = 1 - w
        out.append((min(1, max(0, x)) * span, min(1, max(0, y)) * span, w, h))
    return out
