"""What the editor can change about a recording, and the time maths for it.

Everything is kept in the RECORDING's own time (seconds into take.mkv),
never in the finished video's time, so cutting a piece out of the middle
never shifts anything else. Two functions translate between the clocks.

The document is a plain dict so undo can keep whole copies cheaply.
"""

import copy
import json

MIN_TRIM = 0.2   # the kept part is never shorter than this
MIN_CUT = 0.1    # shorter cuts are dropped as accidental


def new_document(duration):
    return {
        "version": 1,
        "trim": [0.0, float(duration)],
        "cuts": [],                      # [[start, end], ...] in recording time
        "gain": {"system": 1.0, "mic": 1.0},
        "muted": {"system": False, "mic": False},
        # Seconds the system sound is moved later (+) or earlier (-) against
        # the microphone, on top of what the recording already corrected for
        # the speakers' delay (take.json "system_delay").
        "sync": {"system": 0.0},
        # Zoom on click. Segments are in recording time; focus None follows
        # the pointer, [x, y] looks at that spot (fractions of the picture).
        "zoom": {"enabled": True, "amount": 1.8, "segments": [], "generated": False},
        # Background-noise removal on the microphone track.
        "denoise": {"mic": False, "strength": "normal"},
        # Background and shape of the finished video (see compose.py).
        "style": _default_style(),
        # Overlays, in recording time. Texts and images sit at one of nine
        # places on the finished frame; blurs cover a part of the recording.
        "texts": [],    # {id, start, end, text, size, color, anchor}
        "images": [],   # {id, start, end, file, size, opacity, anchor}
        "blurs": [],    # {id, start, end, rect: [x, y, w, h], strength}
        # The drawn pointer. "drawn" is set when the recording left the real
        # pointer out (the editor then always has to draw one back in).
        "pointer": {"drawn": False, "show": True, "size": 1.0, "smoothing": "smooth",
                    "rings": True},
        # How the finished file is made. The preview always plays at 1x.
        "export": dict(EXPORT_DEFAULTS),
    }


SPEED_RANGE = (0.25, 4.0)
SPEED_PRESETS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
QUALITIES = ("high", "balanced", "small")
FIT_SIZES_MB = (0, 10, 25, 50, 100)          # 0: no limit
GIF_SIZES = {"small": 420, "medium": 600, "large": 800}   # longest edge
GIF_FPS = (8, 12, 15)
EXPORT_DEFAULTS = {"speed": 1.0, "quality": "high", "fit_mb": 0,
                   "gif_size": "medium", "gif_fps": 12}


def normalize_export(given):
    ex = {**EXPORT_DEFAULTS, **(given or {})}
    try:
        speed = float(ex["speed"])
    except (TypeError, ValueError):
        speed = 1.0
    ex["speed"] = round(max(SPEED_RANGE[0], min(SPEED_RANGE[1], speed)), 2)
    if ex["quality"] not in QUALITIES:
        ex["quality"] = "high"
    try:
        ex["fit_mb"] = max(0, int(ex["fit_mb"]))
    except (TypeError, ValueError):
        ex["fit_mb"] = 0
    if ex["gif_size"] not in GIF_SIZES:
        ex["gif_size"] = "medium"
    if ex["gif_fps"] not in GIF_FPS:
        ex["gif_fps"] = 12
    return {k: ex[k] for k in EXPORT_DEFAULTS}


def _default_style():
    from .compose import default_style
    return default_style()


def load(path, duration):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return new_document(duration)
    base = new_document(duration)
    base.update({k: v for k, v in doc.items() if k in base})
    return normalize(base, duration)


def save(path, doc):
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)


def clone(doc):
    return copy.deepcopy(doc)


SYNC_RANGE = (-1.0, 1.0)


def normalize(doc, duration):
    """Clamps trim, sorts and merges cuts, drops tiny or out-of-range ones."""
    doc = clone(doc)
    sync = (doc.get("sync") or {}).get("system", 0.0)
    try:
        sync = float(sync)
    except (TypeError, ValueError):
        sync = 0.0
    doc["sync"] = {"system": round(max(SYNC_RANGE[0], min(SYNC_RANGE[1], sync)), 3)}
    start, end = doc["trim"]
    start = max(0.0, min(float(start), duration))
    end = max(0.0, min(float(end), duration))
    if end - start < MIN_TRIM:
        end = min(duration, start + MIN_TRIM)
        start = max(0.0, end - MIN_TRIM)
    doc["trim"] = [start, end]

    # Cuts are kept even where they fall outside the trim: dragging a trim
    # handle across a cut and back must not lose it.
    cuts = []
    for a, b in sorted((min(a, b), max(a, b)) for a, b in doc["cuts"]):
        a, b = max(a, 0.0), min(b, duration)
        if b - a < MIN_CUT:
            continue
        if cuts and a <= cuts[-1][1] + 1e-6:
            cuts[-1][1] = max(cuts[-1][1], b)
        else:
            cuts.append([a, b])
    doc["cuts"] = cuts
    # Never let cuts remove everything that's left.
    while cuts and finished_duration(doc) < MIN_TRIM:
        cuts.pop()
    from . import motion
    zoom = {**new_document(duration)["zoom"], **(doc.get("zoom") or {})}
    zoom["amount"] = max(motion.AMOUNT_RANGE[0], min(motion.AMOUNT_RANGE[1], float(zoom["amount"])))
    zoom["segments"] = motion.normalize_segments(zoom["segments"], duration)
    zoom["enabled"] = bool(zoom["enabled"])
    doc["zoom"] = zoom
    style = _default_style()
    given = doc.get("style") or {}
    style["background"].update(given.get("background") or {})
    style["aspect"] = given.get("aspect", "original")
    from .compose import ASPECTS, ANCHORS, TEXT_COLORS
    if style["aspect"] not in ASPECTS:
        style["aspect"] = "original"
    for key in ("padding", "radius", "blur"):
        style["background"][key] = max(0.0, min(1.0, float(style["background"][key])))
    doc["style"] = style

    def timed(items, defaults):
        out = []
        for item in items or []:
            item = {**defaults, **item}
            item["start"] = max(0.0, min(float(item["start"]), duration))
            item["end"] = max(item["start"] + 0.2, min(float(item["end"]), duration))
            if item["end"] - item["start"] >= 0.2:
                out.append(item)
        return sorted(out, key=lambda i: i["start"])

    doc["texts"] = timed(doc.get("texts"), {"text": "Your text here", "size": 0.06,
                                            "color": "white", "anchor": "bottom"})
    for t in doc["texts"]:
        t["size"] = max(0.03, min(0.16, float(t["size"])))
        t["color"] = t["color"] if t["color"] in TEXT_COLORS else "white"
        t["anchor"] = t["anchor"] if t["anchor"] in ANCHORS else "bottom"
    doc["images"] = [i for i in timed(doc.get("images"), {"size": 0.18, "opacity": 1.0,
                                                          "anchor": "topTrailing"}) if i.get("file")]
    for i in doc["images"]:
        i["size"] = max(0.04, min(0.6, float(i["size"])))
        i["opacity"] = max(0.05, min(1.0, float(i["opacity"])))
        i["anchor"] = i["anchor"] if i["anchor"] in ANCHORS else "topTrailing"
    doc["blurs"] = timed(doc.get("blurs"), {"rect": [0.35, 0.4, 0.3, 0.2], "strength": 3})
    for b in doc["blurs"]:
        x, y, w, h = (float(v) for v in b["rect"])
        x, y = max(0.0, min(0.99, x)), max(0.0, min(0.99, y))
        b["rect"] = [x, y, max(0.01, min(1 - x, w)), max(0.01, min(1 - y, h))]
        b["strength"] = max(1, min(5, int(b["strength"])))

    ptr = {**new_document(duration)["pointer"], **(doc.get("pointer") or {})}
    ptr["drawn"], ptr["show"], ptr["rings"] = bool(ptr["drawn"]), bool(ptr["show"]), bool(ptr["rings"])
    ptr["size"] = max(0.5, min(2.0, float(ptr["size"])))
    if ptr["smoothing"] not in ("off", "light", "smooth", "cinematic"):
        ptr["smoothing"] = "smooth"
    doc["pointer"] = ptr

    dn = {**new_document(duration)["denoise"], **(doc.get("denoise") or {})}
    dn["mic"] = bool(dn["mic"])
    dn["strength"] = dn["strength"] if dn["strength"] in ("normal", "strong") else "normal"
    doc["denoise"] = dn
    for key in ("system", "mic"):
        doc["gain"][key] = max(0.0, min(2.0, float(doc["gain"].get(key, 1.0))))
        doc["muted"][key] = bool(doc["muted"].get(key, False))
    doc["export"] = normalize_export(doc.get("export"))
    return doc


def kept_segments(doc):
    """The parts of the recording that survive, in order: [(start, end), ...]."""
    start, end = doc["trim"]
    segments = []
    pos = start
    for a, b in doc["cuts"]:
        if a >= end:
            break
        if a > pos:
            segments.append((pos, a))
        pos = max(pos, b)
    if end > pos:
        segments.append((pos, end))
    return segments


def finished_duration(doc):
    """Length of the edit at 1x, the clock the timeline and preview use."""
    return sum(b - a for a, b in kept_segments(doc))


def output_duration(doc):
    """Length of the saved file, after the export speed."""
    return finished_duration(doc) / (doc.get("export") or EXPORT_DEFAULTS)["speed"]


def to_finished(doc, t):
    """Recording time -> finished-video time, or None if t was cut out."""
    elapsed = 0.0
    for a, b in kept_segments(doc):
        if t < a:
            return None
        if t <= b:
            return elapsed + (t - a)
        elapsed += b - a
    return None


def to_recording(doc, f):
    """Finished-video time -> recording time (clamped to the kept part)."""
    segments = kept_segments(doc)
    if not segments:
        return doc["trim"][0]
    elapsed = 0.0
    for a, b in segments:
        if f <= elapsed + (b - a):
            return a + max(0.0, f - elapsed)
        elapsed += b - a
    return segments[-1][1]


def snap_to_kept(doc, t):
    """The nearest moment at or after t that still exists in the video."""
    segments = kept_segments(doc)
    for a, b in segments:
        if t < a:
            return a
        if t <= b:
            return t
    return segments[-1][1] if segments else doc["trim"][0]


def cut_at(doc, t):
    """Index of the cut containing recording time t, or None.

    The end is not part of the cut: playback resumes exactly there."""
    for i, (a, b) in enumerate(doc["cuts"]):
        if a <= t < b:
            return i
    return None


def is_unedited(doc, duration):
    """True when saving the recording as is would give the same video."""
    return (doc["trim"][0] <= 0.01 and doc["trim"][1] >= duration - 0.01 and not doc["cuts"]
            and not has_zoom(doc) and not (doc.get("denoise") or {}).get("mic")
            and not needs_compose(doc)
            and normalize_export(doc.get("export"))["speed"] == 1.0
            and normalize_export(doc.get("export"))["quality"] == "high"
            and not normalize_export(doc.get("export"))["fit_mb"]
            and all(abs(doc["gain"][k] - 1.0) < 1e-3 and not doc["muted"][k]
                    for k in ("system", "mic"))
            and abs((doc.get("sync") or {}).get("system", 0.0)) < 1e-3)


# ---- the timeline's own clock -------------------------------------------------
# The timeline shows the recording with cut parts removed and the rest joined
# up ("view time"). Trimmed ends stay visible there, darkened, so they can be
# dragged back out.

def view_segments(doc, duration):
    """Parts of the recording that are not cut, ignoring the trim."""
    segments, pos = [], 0.0
    for a, b in doc["cuts"]:
        if a > pos:
            segments.append((pos, a))
        pos = max(pos, b)
    if duration > pos:
        segments.append((pos, duration))
    return segments


def view_duration(doc, duration):
    return sum(b - a for a, b in view_segments(doc, duration))


def to_view(doc, duration, t):
    """Recording time -> timeline position. Inside a cut maps to its seam."""
    v = 0.0
    for a, b in view_segments(doc, duration):
        if t <= a:
            return v
        if t <= b:
            return v + (t - a)
        v += b - a
    return v


def from_view(doc, duration, v):
    """Timeline position -> recording time."""
    segments = view_segments(doc, duration)
    acc = 0.0
    for a, b in segments:
        if v <= acc + (b - a):
            return a + max(0.0, v - acc)
        acc += b - a
    return segments[-1][1] if segments else 0.0


def seams(doc, duration):
    """Where each cut sits on the timeline: [(view position, cut index)]."""
    return [(to_view(doc, duration, a), i) for i, (a, _b) in enumerate(doc["cuts"])]


def has_zoom(doc):
    z = doc.get("zoom") or {}
    return bool(z.get("enabled") and z.get("segments"))


def needs_compose(doc):
    """True when the picture itself is changed: background, shape or overlays."""
    style = doc.get("style") or {}
    if (doc.get("pointer") or {}).get("drawn"):
        return True  # the pointer has to be drawn back in (or is hidden on purpose)
    return bool(style.get("background", {}).get("kind", "none") != "none"
                or style.get("aspect", "original") != "original"
                or doc.get("texts") or doc.get("images") or doc.get("blurs"))


def new_id():
    import uuid
    return uuid.uuid4().hex[:8]
