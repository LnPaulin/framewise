"""The drawn pointer: where it is, how it moves, and the click ring.

Recordings leave the real pointer out (the portal's hidden cursor mode)
and the editor draws it back:

- Smoothing is a zero-lag low-pass run forwards and backwards over the
  finished path. Around every click it blends back to the real position,
  and for the whole press-to-release it IS the real position, so the
  pointer is exactly on what was clicked and dragged things stay under it.
  Shaking the mouse is a gesture, so inside a shake the raw path is kept.
- After 2.5 s without moving it fades out, and comes back just before it
  moves again (possible because the whole path is known in advance).
- Each click presses the pointer in a little and leaves a thin ring that
  grows and fades over 0.4 s.

Positions are fractions (0..1) of the recorded area. Times are recording time.
"""

import math

SMOOTHING = {"off": 0.0, "light": 4.0, "smooth": 2.0, "cinematic": 1.2}  # cutoff, Hz
FILTFILT_WIDENING = 1.553773974
CLICK_ANCHOR = 0.12
SHAKE_WINDOW, SHAKE_DISTANCE, SHAKE_REVERSALS = 0.1, 0.015, 3
IDLE_DELAY, IDLE_FADE_OUT, IDLE_FADE_IN, IDLE_TRAVEL = 2.5, 0.4, 0.25, 0.0006
PRESS_WINDOW, PRESS_SCALE = 0.13, 0.8
RING_DURATION, RING_RADIUS = 0.4, 22.0
SIZE_RANGE = (0.5, 2.0)


def smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def pixel_size(src_h, user_scale=1.0, system_scale=1.0):
    """Pointer height in the recording's pixels (tied to the
    picture's height, so it reads the same on a small area and a big screen)."""
    base = max(24.0, src_h * 0.028)
    return round(base * max(SIZE_RANGE[0], min(SIZE_RANGE[1], user_scale)) * max(1.0, system_scale))


def clicks_from_samples(samples):
    """[(time, is_down)] from samples [(t, x, y, down, ...)]."""
    out, was = [], 0
    for s in samples:
        down = s[3]
        if down != was:
            out.append((s[0], bool(down)))
            was = down
    return out


def frames(samples, n, fps):
    """Per-frame raw position and visibility, from samples [(t, x, y, down, [visible])]."""
    pts, vis, j = [], [], 0
    if not samples:
        return [(0.5, 0.5)] * n, [False] * n
    for i in range(n):
        t = i / fps
        while j + 1 < len(samples) and samples[j + 1][0] <= t:
            j += 1
        a = samples[j]
        if j + 1 < len(samples) and samples[j + 1][0] > a[0] and t > a[0]:
            b = samples[j + 1]
            k = (t - a[0]) / (b[0] - a[0])
            pts.append((a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k))
        else:
            pts.append((a[1], a[2]))
        vis.append(bool(a[4]) if len(a) > 4 else True)
    return pts, vis


def _filtfilt(values, alpha):
    if len(values) < 2 or not (0 < alpha < 1):
        return list(values)
    pad = min(len(values), max(1, round(3 / alpha)))
    p = [values[0]] * pad + list(values) + [values[-1]] * pad
    carry = p[0]
    for i in range(len(p)):
        carry += alpha * (p[i] - carry)
        p[i] = carry
    carry = p[-1]
    for i in range(len(p) - 1, -1, -1):
        carry += alpha * (p[i] - carry)
        p[i] = carry
    return p[pad:pad + len(values)]


def _anchor_weight(t, clicks):
    """1 while pressed, eased to 0 within CLICK_ANCHOR seconds of a click."""
    weight, pressed = 0.0, None
    for ct, down in clicks:
        if pressed is None and ct > t + CLICK_ANCHOR:
            break
        if down:
            pressed = ct
        elif pressed is not None:
            if pressed <= t <= ct:
                weight = 1.0
            pressed = None
        d = abs(t - ct)
        if d <= CLICK_ANCHOR:
            weight = max(weight, smoothstep(1 - d / CLICK_ANCHOR))
    if clicks and clicks[-1][1] and t >= clicks[-1][0]:
        return 1.0  # recording ended mid-drag
    return weight


def _shaking(points, i, fps):
    half = max(1, int(SHAKE_WINDOW * fps))
    lo, hi = max(0, i - half), min(len(points), i + half + 1)
    travel, reversals, prev = 0.0, 0, 0
    for k in range(lo + 1, hi):
        dx = points[k][0] - points[k - 1][0]
        travel += abs(dx) + abs(points[k][1] - points[k - 1][1])
        sign = (dx > 1e-5) - (dx < -1e-5)
        if sign and prev and sign != prev:
            reversals += 1
        if sign:
            prev = sign
    return travel > SHAKE_DISTANCE * 4 and reversals >= SHAKE_REVERSALS


def smoothed(points, clicks, smoothing, fps):
    cutoff = SMOOTHING.get(smoothing, 2.0)
    if cutoff <= 0 or len(points) < 2:
        return list(points)
    alpha = 1 - math.exp(-2 * math.pi * cutoff * FILTFILT_WIDENING / fps)
    xs = _filtfilt([p[0] for p in points], alpha)
    ys = _filtfilt([p[1] for p in points], alpha)
    out = []
    for i, (sx, sy) in enumerate(zip(xs, ys)):
        rx, ry = points[i]
        if _shaking(points, i, fps):
            out.append((rx, ry))
            continue
        w = _anchor_weight(i / fps, clicks)
        out.append((sx + (rx - sx) * w, sy + (ry - sy) * w))
    return out


def idle_opacity(points, fps):
    """Fades the pointer out after it sits still, and back in before it moves."""
    n = len(points)
    if n < 2:
        return [1.0] * n
    step = 1.0 / fps
    still = [0.0] * n
    for i in range(1, n):
        moved = math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]) > IDLE_TRAVEL
        still[i] = 0.0 if moved else still[i - 1] + step
    until = [math.inf] * n
    for i in range(n - 2, -1, -1):
        until[i] = 0.0 if still[i + 1] == 0 else until[i + 1] + step
    out = []
    for i in range(n):
        waking = 1 - until[i] / IDLE_FADE_IN if until[i] <= IDLE_FADE_IN else 0.0
        sleeping = 0.0 if still[i] <= IDLE_DELAY else min(1.0, (still[i] - IDLE_DELAY) / IDLE_FADE_OUT)
        out.append(1 - smoothstep(max(0.0, sleeping - waking)))
    return out


def press_scale(t, clicks):
    """The pointer presses in a little around each click."""
    weight, pressed = 0.0, None
    for ct, down in clicks:
        if pressed is None and ct > t + PRESS_WINDOW:
            break
        if down:
            pressed = ct
            if ct - PRESS_WINDOW <= t <= ct:
                weight = max(weight, smoothstep((t - (ct - PRESS_WINDOW)) / PRESS_WINDOW))
        elif pressed is not None:
            if pressed <= t <= ct:
                weight = 1.0
            if ct < t <= ct + PRESS_WINDOW:
                weight = max(weight, smoothstep(1 - (t - ct) / PRESS_WINDOW))
            pressed = None
    if clicks and clicks[-1][1] and t >= clicks[-1][0]:
        weight = 1.0
    return 1 - (1 - PRESS_SCALE) * min(1.0, weight)


def ring(t, clicks, raw_at):
    """(progress 0..1, x, y) of the newest ring at t, or None. raw_at(time) -> (x, y)."""
    best = None
    for ct, down in clicks:
        elapsed = t - ct
        if elapsed < 0:
            break
        if down and elapsed <= RING_DURATION:
            p = elapsed / RING_DURATION
            if best is None or p < best[0]:
                best = (p, *raw_at(ct))
    return best


def ring_shape(progress, pointer_px):
    """(radius, line width, alpha) of a ring, in recording pixels."""
    unit = pointer_px / 34
    eased = 1 - (1 - progress) ** 3
    return (RING_RADIUS * unit * eased, max(0.6, 2.4 * (1 - progress)) * unit,
            0.35 * (1 - progress * progress))


def track(samples, duration, fps, smoothing="smooth"):
    """Everything needed to draw the pointer at every frame.

    Returns a list of (x, y, opacity, scale) per frame (opacity 0 = not
    shown) and the ring per frame: None or (progress, x, y).
    """
    n = max(1, int(math.ceil(duration * fps)))
    raw, visible = frames(samples, n, fps)
    clicks = clicks_from_samples(samples)
    path = smoothed(raw, clicks, smoothing, fps)
    idle = idle_opacity(raw, fps)

    def raw_at(t):
        return raw[max(0, min(n - 1, int(round(t * fps))))]

    out, rings = [], []
    for i in range(n):
        t = i / fps
        x, y = path[i]
        inside = -0.02 <= x <= 1.02 and -0.02 <= y <= 1.02
        opacity = idle[i] if (visible[i] and inside) else 0.0
        out.append((x, y, opacity, press_scale(t, clicks)))
        rings.append(ring(t, clicks, raw_at))
    return out, rings


# ---- drawing (shared by the preview and the export) ----------------------------------

ARROW = [(0, 0), (0, 17), (4.2, 13.3), (6.9, 19.6), (9.6, 18.4), (7, 12.3), (12.6, 12.3)]
ARROW_BOX = (14, 21)  # units the outline above is drawn in


def draw_arrow(cr, height, alpha=1.0):
    """A crisp arrow with its tip at (0, 0), `height` pixels tall."""
    k = height / ARROW_BOX[1]
    cr.save()
    cr.scale(k, k)
    cr.move_to(*ARROW[0])
    for p in ARROW[1:]:
        cr.line_to(*p)
    cr.close_path()
    cr.set_line_join(1)  # round
    cr.set_source_rgba(0, 0, 0, 0.35 * alpha)  # soft shadow
    cr.save()
    cr.translate(0.6, 0.9)
    cr.fill_preserve()
    cr.restore()
    cr.new_path()
    cr.move_to(*ARROW[0])
    for p in ARROW[1:]:
        cr.line_to(*p)
    cr.close_path()
    cr.set_source_rgba(0, 0, 0, alpha)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.set_line_width(1.6)
    cr.stroke()
    cr.restore()


def arrow_png(path, height):
    """The arrow as a PNG (padding around it for the outline). Tip at (pad, pad)."""
    import cairo
    pad = max(2, int(height * 0.12))
    w = int(height * ARROW_BOX[0] / ARROW_BOX[1]) + 2 * pad + 2
    h = int(height) + 2 * pad + 2
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    cr.translate(pad, pad)
    draw_arrow(cr, height)
    surface.write_to_png(str(path))
    return pad, (w, h)


# ---- frame-exact positions for ffmpeg -------------------------------------------------
# ffmpeg's runtime commands reach a filter a few frames before the frame they
# belong to, which is visible on a pointer. Instead the path goes into the
# overlay's x/y as an expression of the frame's own time: the path is
# simplified to the few points that keep it within a pixel, and written as a
# balanced tree of if(), so even long recordings stay cheap to evaluate.

def simplify(times, xs, ys, tolerance=0.75):
    """Indices of the points that keep a piecewise-linear path within tolerance."""
    n = len(times)
    if n <= 2:
        return list(range(n))
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ta, tb = times[a], times[b]
        worst, worst_k = -1.0, None
        for k in range(a + 1, b):
            f = (times[k] - ta) / (tb - ta) if tb > ta else 0.0
            dx = abs(xs[a] + (xs[b] - xs[a]) * f - xs[k])
            dy = abs(ys[a] + (ys[b] - ys[a]) * f - ys[k])
            d = max(dx, dy)
            if d > worst:
                worst, worst_k = d, k
        if worst > tolerance:
            keep.add(worst_k)
            stack += [(a, worst_k), (worst_k, b)]
    return sorted(keep)


def linear_expr(knots):
    """knots [(t, v)] -> ffmpeg expression of t, linear between knots."""
    def build(lo, hi):
        if hi - lo <= 1:
            (ta, va), (tb, vb) = knots[lo], knots[hi]
            if tb <= ta or abs(vb - va) < 0.01:
                return f"{va:.1f}"
            return f"({va:.1f}+{(vb - va) / (tb - ta):.4f}*(t-{ta:.4f}))"
        mid = (lo + hi) // 2
        return f"if(lt(t,{knots[mid][0]:.4f}),{build(lo, mid)},{build(mid, hi)})"
    if len(knots) == 1:
        return f"{knots[0][1]:.1f}"
    first, last = knots[0], knots[-1]
    return (f"if(lt(t,{first[0]:.4f}),{first[1]:.1f},if(gte(t,{last[0]:.4f}),{last[1]:.1f},"
            f"{build(0, len(knots) - 1)}))")


def step_expr(steps, default=-999):
    """steps [(t_start, v)] -> expression holding each value until the next."""
    if not steps:
        return str(default)

    def build(lo, hi):
        if hi - lo <= 1:
            return f"{steps[lo][1]}"
        mid = (lo + hi) // 2
        return f"if(lt(t,{steps[mid][0]:.4f}),{build(lo, mid)},{build(mid, hi)})"
    return f"if(lt(t,{steps[0][0]:.4f}),{default},{build(0, len(steps))})" if len(steps) > 1 else \
        f"if(lt(t,{steps[0][0]:.4f}),{default},{steps[0][1]})"
