"""The editor's timeline: the recording as a strip of frames.

Cut parts are taken out of the strip and the parts around them join up, so
the strip shows the video as it will be. An orange seam marks each cut;
clicking it puts that part back. Trimmed-off ends stay visible but dark, so
their red handles can be dragged back out.

- Drag on the strip to scrub.
- Drag either red handle to trim.
- Hold Shift and drag to pick a stretch to cut out (the picture follows).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from . import edit_model as em  # noqa: E402

HANDLE_W = 12
HIT = 10        # pixels around a handle that grab it
SEAM_HIT = 6    # pixels around a seam that click it


def _rect(x, y, w, h):
    r = Graphene.Rect()
    r.init(x, y, w, h)
    return r


def _rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


SHADE = _rgba(0, 0, 0, 0.62)
ACCENT = _rgba(0.898, 0.282, 0.302)          # the recorder's red
SELECT = _rgba(0.898, 0.282, 0.302, 0.30)
SEAM = _rgba(1.0, 0.62, 0.04, 0.95)          # orange seams where cuts were made
SUGGEST = {"silence": _rgba(1.0, 0.75, 0.1, 0.42),    # suggested cuts: pauses
           "filler": _rgba(0.62, 0.36, 1.0, 0.55)}    # suggested cuts: um, uh, hmm
WHITE = _rgba(1, 1, 1)
BG = _rgba(0.12, 0.12, 0.14)


class Filmstrip(Gtk.Widget):
    """Callbacks (times are recording times):
    on_scrub(t), on_trim(start, end, done), on_select((a, b) or None),
    on_restore(cut_index)
    """

    def __init__(self, duration):
        super().__init__(hexpand=True, height_request=72, focusable=True)
        self.duration = max(0.001, duration)
        self.doc = None
        self.position = 0.0
        self.thumbs = []          # [(time, Gdk.Texture)] sorted by time
        self.selection = None     # (a, b) in recording time, picked with Shift+drag
        self.suggestions = {}     # kind -> [(a, b)] in recording time, e.g. pauses to cut
        self.on_scrub = self.on_trim = self.on_select = self.on_restore = None
        self.on_suggestion = None  # (kind, (a, b)) when a highlighted part is clicked
        self._drag = None

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._hover)
        self.add_controller(motion)

    # ---- data ----------------------------------------------------------------

    def set_document(self, doc):
        self.doc = doc
        self.queue_draw()

    def set_position(self, t):
        self.position = t
        self.queue_draw()

    def set_thumbnails(self, thumbs):
        self.thumbs = sorted(thumbs, key=lambda p: p[0])
        self.queue_draw()

    def set_suggestions(self, ranges, kind="silence"):
        self.suggestions[kind] = list(ranges)
        self.queue_draw()

    def clear_selection(self):
        self.selection = None
        self.queue_draw()

    # ---- geometry: x <-> timeline (view) time <-> recording time ---------------------

    def _vdur(self):
        return max(0.001, em.view_duration(self.doc, self.duration))

    def _xv(self, v):
        w = self.get_width() - 2 * HANDLE_W
        return HANDLE_W + w * (v / self._vdur())

    def _x(self, t):
        return self._xv(em.to_view(self.doc, self.duration, t))

    def _t(self, x):
        w = max(1, self.get_width() - 2 * HANDLE_W)
        v = max(0.0, min(self._vdur(), (x - HANDLE_W) / w * self._vdur()))
        return em.from_view(self.doc, self.duration, v)

    def _seam_at(self, x):
        for v, i in em.seams(self.doc, self.duration):
            if abs(self._xv(v) - x) <= SEAM_HIT:
                return i
        return None

    def _suggestion_at(self, x):
        for kind, ranges in self.suggestions.items():
            for a, b in ranges:
                if self._x(a) - 2 <= x <= self._x(b) + 2:
                    return kind, (a, b)
        return None

    def _hover(self, _c, x, _y):
        if not self.doc:
            return
        start, end = self.doc["trim"]
        near_handle = (abs(x - (self._x(start) - HANDLE_W / 2)) <= HIT
                       or abs(x - (self._x(end) + HANDLE_W / 2)) <= HIT)
        if near_handle:
            name = "ew-resize"
        elif self._seam_at(x) is not None:
            name = "pointer"
            tip = "Click to put this part back"
        elif self._suggestion_at(x):
            name = "pointer"
            kind = self._suggestion_at(x)[0]
            tip = ("Click to cut this filler word" if kind == "filler"
                   else "Click to cut this pause")
        else:
            name = "text"
        if name != "pointer":
            tip = None
        self.set_cursor(Gdk.Cursor.new_from_name(name))
        self.set_tooltip_text(tip)

    # ---- input ----------------------------------------------------------------------

    def _begin(self, gesture, x, _y):
        self.grab_focus()
        start, end = self.doc["trim"]
        shift = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        if abs(x - (self._x(start) - HANDLE_W / 2)) <= HIT:
            self._drag = ["trim-start"]
        elif abs(x - (self._x(end) + HANDLE_W / 2)) <= HIT:
            self._drag = ["trim-end"]
        elif not shift and self._seam_at(x) is not None:
            self._drag = ["seam", self._seam_at(x)]
        elif shift:
            t = self._t(x)
            self._drag = ["select", t]
            self.selection = (t, t)
            self._scrub(t)
        else:
            self._drag = ["scrub", x]
            self.selection = None
            if self.on_select:
                self.on_select(None)
            self._scrub(self._t(x))
        self.queue_draw()

    def _scrub(self, t):
        start, end = self.doc["trim"]
        if self.on_scrub:
            self.on_scrub(em.snap_to_kept(self.doc, max(start, min(end, t))))

    def _update(self, gesture, dx, _dy):
        if not self._drag:
            return
        _ok, x0, _y0 = gesture.get_start_point()
        x = x0 + dx
        kind = self._drag[0]
        start, end = self.doc["trim"]
        if kind == "trim-start" and self.on_trim:
            self.on_trim(min(self._t(x), end - em.MIN_TRIM), end, False)
        elif kind == "trim-end" and self.on_trim:
            self.on_trim(start, max(self._t(x), start + em.MIN_TRIM), False)
        elif kind == "select":
            t = self._t(x)
            self.selection = (self._drag[1], t)
            self._scrub(t)  # the picture follows, so the cut lands on the exact moment
        elif kind == "scrub":
            self._scrub(self._t(x))
        self.queue_draw()

    def _end(self, _gesture, dx, _dy):
        if not self._drag:
            return
        kind = self._drag[0]
        if kind in ("trim-start", "trim-end") and self.on_trim:
            start, end = self.doc["trim"]
            self.on_trim(start, end, True)
        elif kind == "select":
            a, b = sorted(self.selection)
            self.selection = (a, b) if b - a >= em.MIN_CUT else None
            if self.on_select:
                self.on_select(self.selection)
        elif kind == "scrub" and abs(dx) < 4 and self.on_suggestion:
            hit = self._suggestion_at(self._drag[1])
            if hit:
                self.on_suggestion(*hit)
        elif kind == "seam" and abs(dx) < 4 and self.on_restore:
            self.on_restore(self._drag[1])
        self._drag = None
        self.queue_draw()

    # ---- drawing ------------------------------------------------------------------

    def _thumb_at(self, t):
        best = self.thumbs[0][1]
        for time, tex in self.thumbs:
            if time > t:
                break
            best = tex
        return best

    def do_snapshot(self, snap):
        width, height = self.get_width(), self.get_height()
        if not self.doc or width <= 2 * HANDLE_W:
            return
        top, strip_h = 4, height - 8
        x0, x1 = self._xv(0), self._xv(self._vdur())
        frame = Gsk.RoundedRect()
        frame.init_from_rect(_rect(x0, top, x1 - x0, strip_h), 6)
        snap.push_rounded_clip(frame)
        snap.append_color(BG, _rect(x0, top, x1 - x0, strip_h))

        # Frames: tile each joined part with the frames from its own time.
        if self.thumbs:
            tex0 = self.thumbs[0][1]
            tw = max(8.0, tex0.get_width() * strip_h / max(1, tex0.get_height()))
            for a, b in em.view_segments(self.doc, self.duration):
                xa, xb = self._x(a), self._x(b)
                snap.push_clip(_rect(xa, top, xb - xa, strip_h))
                x = xa
                while x < xb:
                    t = self._t(x + tw / 2)
                    snap.append_texture(self._thumb_at(t), _rect(x, top, tw, strip_h))
                    x += tw
                snap.pop()

        start, end = self.doc["trim"]
        xs, xe = self._x(start), self._x(end)
        snap.append_color(SHADE, _rect(x0, top, xs - x0, strip_h))
        snap.append_color(SHADE, _rect(xe, top, x1 - xe, strip_h))

        for kind, ranges in self.suggestions.items():
            for a, b in ranges:
                xa, xb = self._x(a), self._x(b)
                if xb - xa >= 1:
                    snap.append_color(SUGGEST[kind], _rect(xa, top, max(2, xb - xa), strip_h))
        if self.selection:
            a, b = sorted(self.selection)
            xa, xb = self._x(a), self._x(b)
            snap.append_color(SELECT, _rect(xa, top, xb - xa, strip_h))
            self._outline(snap, xa, top, xb - xa, strip_h, ACCENT, 1.5)
        snap.pop()

        # Seams where parts were cut out.
        for v, _i in em.seams(self.doc, self.duration):
            sx = self._xv(v)
            snap.append_color(SEAM, _rect(sx - 1.5, top - 2, 3, strip_h + 4))

        # The kept part's frame and its handles.
        self._outline(snap, xs, top, xe - xs, strip_h, ACCENT, 2)
        for hx in (xs - HANDLE_W, xe):
            handle = Gsk.RoundedRect()
            handle.init_from_rect(_rect(hx, top, HANDLE_W, strip_h), 4)
            snap.push_rounded_clip(handle)
            snap.append_color(ACCENT, _rect(hx, top, HANDLE_W, strip_h))
            snap.pop()
            snap.append_color(WHITE, _rect(hx + HANDLE_W / 2 - 1, top + strip_h * 0.32, 2, strip_h * 0.36))

        # Playhead.
        px = self._x(self.position)
        snap.append_color(_rgba(0, 0, 0, 0.5), _rect(px - 2, 0, 4, height))
        snap.append_color(WHITE, _rect(px - 1, 0, 2, height))

    @staticmethod
    def _outline(snap, x, y, w, h, color, width=2):
        rr = Gsk.RoundedRect()
        rr.init_from_rect(_rect(x, y, max(0, w), h), 0)
        snap.append_border(rr, [width] * 4, [color] * 4)


def make_thumbnails(take, out_dir, crop_px, on_done, count=24, fps=60):
    """Extracts keyframes as small JPEGs in the background.

    on_done([(time, Gdk.Texture)]) on the main loop. Uses keyframes only,
    which is fast: about one every two seconds of recording.
    """
    from pathlib import Path
    from gi.repository import Gio

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    vf = (f"crop={crop_px[2]}:{crop_px[3]}:{crop_px[0]}:{crop_px[1]}," if crop_px else "") + "scale=-2:96"
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-skip_frame", "nokey", "-i", str(take),
           "-vf", vf, "-fps_mode", "passthrough", "-frame_pts", "1", "-q:v", "4",
           str(out / "%010d.jpg")]
    try:
        proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDERR_SILENCE)
    except GLib.Error:
        on_done([])
        return

    def finished(p, result):
        try:
            p.wait_finish(result)
        except GLib.Error:
            pass
        thumbs = []
        # -frame_pts names each file by its frame number (the encoder counts in
        # 1/fps steps), so frame / fps is its time.
        for f in sorted(out.glob("*.jpg")):
            try:
                thumbs.append((int(f.stem) / fps, Gdk.Texture.new_from_filename(str(f))))
            except (GLib.Error, ValueError):
                pass
        if len(thumbs) > count:
            step = len(thumbs) / count
            thumbs = [thumbs[int(i * step)] for i in range(count)]
        if thumbs:
            thumbs[0] = (0.0, thumbs[0][1])
        on_done(thumbs)

    proc.wait_async(None, finished)


ZOOM_FILL = _rgba(0.36, 0.55, 1.0, 0.85)
ZOOM_FILL_SELECTED = _rgba(0.36, 0.55, 1.0, 1.0)
LANE_BG = _rgba(0.5, 0.5, 0.55, 0.14)
CLICK_DOT = _rgba(0.5, 0.5, 0.55, 0.9)
EDGE_HIT = 7


class ZoomLane(Gtk.Widget):
    """The zooms, as blocks under the filmstrip (same positions as the strip).

    Click an empty spot to add a zoom there. Click a block to select it, drag
    it to move it, drag its ends to make it shorter or longer. Dots mark the
    clicks the recording captured, which is where automatic zooms come from.
    on_change(segments, done), on_select(index or None).
    """

    def __init__(self, strip):
        super().__init__(hexpand=True, height_request=26)
        self.strip = strip
        self.segments = []
        self.clicks = []
        self.selected = None
        self.just_added = False   # True while selecting a zoom that was just added
        self.on_change = self.on_select = None
        self._drag = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._hover)
        self.add_controller(motion)

    def set_segments(self, segments, clicks=None):
        self.segments = [dict(s) for s in segments]
        if clicks is not None:
            self.clicks = clicks
        if self.selected is not None and self.selected >= len(self.segments):
            self.selected = None
        self.queue_draw()

    def select(self, index):
        self.selected = index
        self.queue_draw()
        if self.on_select:
            self.on_select(index)

    # geometry comes from the filmstrip so both line up exactly
    def _x(self, t):
        return self.strip._x(t)

    def _t(self, x):
        return self.strip._t(x)

    def _hit(self, x):
        for i, s in enumerate(self.segments):
            xa, xb = self._x(s["start"]), self._x(s["end"])
            if abs(x - xa) <= EDGE_HIT:
                return i, "start"
            if abs(x - xb) <= EDGE_HIT:
                return i, "end"
            if xa < x < xb:
                return i, "move"
        return None, None

    def _hover(self, _c, x, _y):
        if not self.strip.doc:
            return
        _i, part = self._hit(x)
        name = {"start": "ew-resize", "end": "ew-resize", "move": "grab"}.get(part, "copy")
        self.set_cursor(Gdk.Cursor.new_from_name(name))
        self.set_tooltip_text(None if part else "Click to add a zoom here")

    def _begin(self, _g, x, _y):
        if not self.strip.doc:
            return
        i, part = self._hit(x)
        if i is None:
            t = self._t(x)
            self._drag = ("add", t)
            return
        s = self.segments[i]
        self._drag = (part, i, self._t(x), s["start"], s["end"])
        self.select(i)

    def _update(self, gesture, dx, _dy):
        if not self._drag or self._drag[0] == "add":
            return
        _ok, x0, _y0 = gesture.get_start_point()
        part, i, t0, start, end = self._drag
        dt = self._t(x0 + dx) - t0
        seg = self.segments[i]
        if part == "move":
            length = end - start
            seg["start"] = max(0.0, min(self.strip.duration - length, start + dt))
            seg["end"] = seg["start"] + length
        elif part == "start":
            seg["start"] = max(0.0, min(end - 0.4, start + dt))
        elif part == "end":
            seg["end"] = min(self.strip.duration, max(start + 0.4, end + dt))
        self.queue_draw()
        if self.on_change:
            self.on_change(self.segments, False)

    def _end(self, _g, dx, _dy):
        if not self._drag:
            return
        if self._drag[0] == "add" and abs(dx) < 4:
            from . import motion
            t = self._drag[1]
            start = max(0.0, min(self.strip.duration - motion.ADDED_LENGTH, t - 0.2))
            self.segments.append({"start": round(start, 3),
                                  "end": round(start + motion.ADDED_LENGTH, 3), "focus": None})
            self.segments.sort(key=lambda s: s["start"])
            if self.on_change:
                self.on_change(self.segments, True)
            index = next((k for k, s in enumerate(self.segments) if abs(s["start"] - start) < 1e-3), None)
            self.just_added = True
            self.select(index)
            self.just_added = False
        elif self._drag[0] != "add" and self.on_change:
            self.on_change(self.segments, True)
        self._drag = None

    def do_snapshot(self, snap):
        width, height = self.get_width(), self.get_height()
        if not self.strip.doc or width <= 2 * HANDLE_W:
            return
        x0, x1 = self.strip._xv(0), self.strip._xv(self.strip._vdur())
        bg = Gsk.RoundedRect()
        bg.init_from_rect(_rect(x0, 3, x1 - x0, height - 6), 5)
        snap.push_rounded_clip(bg)
        snap.append_color(LANE_BG, _rect(x0, 3, x1 - x0, height - 6))
        snap.pop()
        for t in self.clicks:
            cx = self._x(t)
            dot = Gsk.RoundedRect()
            dot.init_from_rect(_rect(cx - 2, height / 2 - 2, 4, 4), 2)
            snap.push_rounded_clip(dot)
            snap.append_color(CLICK_DOT, _rect(cx - 2, height / 2 - 2, 4, 4))
            snap.pop()
        for i, s in enumerate(self.segments):
            xa, xb = self._x(s["start"]), self._x(s["end"])
            if xb - xa < 2:
                continue
            block = Gsk.RoundedRect()
            block.init_from_rect(_rect(xa, 4, xb - xa, height - 8), 5)
            snap.push_rounded_clip(block)
            snap.append_color(ZOOM_FILL_SELECTED if i == self.selected else ZOOM_FILL,
                              _rect(xa, 4, xb - xa, height - 8))
            snap.pop()
            if i == self.selected:
                rr = Gsk.RoundedRect()
                rr.init_from_rect(_rect(xa, 4, xb - xa, height - 8), 5)
                snap.append_border(rr, [2] * 4, [WHITE] * 4)
            if xb - xa > 40:
                layout = self.create_pango_layout("⌕ zoom" if not s.get("focus") else "⌕ spot")
                point = Graphene.Point()
                point.init(xa + 8, height / 2 - 8)
                snap.save()
                snap.translate(point)
                snap.append_layout(layout, WHITE)
                snap.restore()
        # Playhead, continuing the filmstrip's.
        px = self.strip._x(self.strip.position)
        snap.append_color(WHITE, _rect(px - 1, 0, 2, height))


LANE_COLORS = {"text": _rgba(0.2, 0.72, 0.45, 0.9), "image": _rgba(0.95, 0.55, 0.2, 0.9),
               "blur": _rgba(0.55, 0.55, 0.62, 0.95)}


class BlockLane(Gtk.Widget):
    """A lane of timed blocks (captions, pictures or blurs) under the filmstrip.

    Same gestures as the zoom lane: click a block to select it, drag it to
    move, drag its ends to resize; click an empty spot to add one there.
    on_change(items, done), on_select(index or None), on_add(time).
    """

    def __init__(self, strip, kind, label):
        super().__init__(hexpand=True, height_request=24)
        self.strip, self.kind, self.label = strip, kind, label
        self.items = []
        self.selected = None
        self.on_change = self.on_select = self.on_add = None
        self._drag = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._hover)
        self.add_controller(motion)

    def set_items(self, items):
        self.items = [dict(i) for i in items]
        if self.selected is not None and self.selected >= len(self.items):
            self.selected = None
        self.set_visible(bool(self.items))
        self.queue_draw()

    def select(self, index, notify=True):
        self.selected = index
        self.queue_draw()
        if notify and self.on_select:
            self.on_select(index)

    def _hit(self, x):
        for i, it in enumerate(self.items):
            xa, xb = self.strip._x(it["start"]), self.strip._x(it["end"])
            if abs(x - xa) <= EDGE_HIT:
                return i, "start"
            if abs(x - xb) <= EDGE_HIT:
                return i, "end"
            if xa < x < xb:
                return i, "move"
        return None, None

    def _hover(self, _c, x, _y):
        _i, part = self._hit(x)
        name = {"start": "ew-resize", "end": "ew-resize", "move": "grab"}.get(part, "copy")
        self.set_cursor(Gdk.Cursor.new_from_name(name))
        self.set_tooltip_text(None if part else f"Click to add {self.label.lower()} here")

    def _begin(self, _g, x, _y):
        i, part = self._hit(x)
        if i is None:
            self._drag = ("add", self.strip._t(x))
            return
        it = self.items[i]
        self._drag = (part, i, self.strip._t(x), it["start"], it["end"])
        self.select(i)

    def _update(self, gesture, dx, _dy):
        if not self._drag or self._drag[0] == "add":
            return
        _ok, x0, _y0 = gesture.get_start_point()
        part, i, t0, start, end = self._drag
        dt = self.strip._t(x0 + dx) - t0
        it = self.items[i]
        if part == "move":
            length = end - start
            it["start"] = max(0.0, min(self.strip.duration - length, start + dt))
            it["end"] = it["start"] + length
        elif part == "start":
            it["start"] = max(0.0, min(end - 0.2, start + dt))
        else:
            it["end"] = min(self.strip.duration, max(start + 0.2, end + dt))
        self.queue_draw()
        if self.on_change:
            self.on_change(self.items, False)

    def _end(self, _g, dx, _dy):
        if not self._drag:
            return
        if self._drag[0] == "add":
            if abs(dx) < 4 and self.on_add:
                self.on_add(self._drag[1])
        elif self.on_change:
            self.on_change(self.items, True)
        self._drag = None

    def do_snapshot(self, snap):
        width, height = self.get_width(), self.get_height()
        if not self.strip.doc or width <= 2 * HANDLE_W:
            return
        x0, x1 = self.strip._xv(0), self.strip._xv(self.strip._vdur())
        bg = Gsk.RoundedRect()
        bg.init_from_rect(_rect(x0, 2, x1 - x0, height - 4), 5)
        snap.push_rounded_clip(bg)
        snap.append_color(LANE_BG, _rect(x0, 2, x1 - x0, height - 4))
        snap.pop()
        color = LANE_COLORS[self.kind]
        for i, it in enumerate(self.items):
            xa, xb = self.strip._x(it["start"]), self.strip._x(it["end"])
            if xb - xa < 2:
                continue
            block = Gsk.RoundedRect()
            block.init_from_rect(_rect(xa, 3, xb - xa, height - 6), 5)
            snap.push_rounded_clip(block)
            snap.append_color(color, _rect(xa, 3, xb - xa, height - 6))
            if xb - xa > 30:
                text = it.get("text") or self.label
                layout = self.create_pango_layout(text[:40])
                point = Graphene.Point()
                point.init(xa + 6, height / 2 - 8)
                snap.save()
                snap.translate(point)
                snap.append_layout(layout, WHITE)
                snap.restore()
            snap.pop()
            if i == self.selected:
                rr = Gsk.RoundedRect()
                rr.init_from_rect(_rect(xa, 3, xb - xa, height - 6), 5)
                snap.append_border(rr, [2] * 4, [WHITE] * 4)
        px = self.strip._x(self.strip.position)
        snap.append_color(WHITE, _rect(px - 1, 0, 2, height))
