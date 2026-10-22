"""The editor's picture, drawn exactly as it will be saved.

Everything is drawn by GTK on the GPU from the same numbers and images the
export uses: the background, the recording's frame with rounded corners,
the zoom, blurred areas, and captions and pictures with their fades.

Two editing modes show the whole recording instead of the finished frame:
picking where a zoom looks, and drawing the area of a blur.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402

from . import compose, motion  # noqa: E402
from . import pointer as ptr  # noqa: E402


def _rect(x, y, w, h):
    r = Graphene.Rect()
    r.init(x, y, max(0.0, w), max(0.0, h))
    return r


def _rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


BLACK = _rgba(0, 0, 0)
DIM = _rgba(0, 0, 0, 0.5)
CHOSEN = _rgba(0.36, 0.55, 1.0)          # the zoom lane's blue
HOVER = _rgba(1, 0.84, 0.04)             # yellow: visible on light and dark
EDGE = _rgba(0, 0, 0, 0.6)
BLUR_EDGE = _rgba(0.9, 0.28, 0.3)


class ZoomView(Gtk.Widget):
    def __init__(self, paintable):
        super().__init__(hexpand=True, vexpand=True)
        self.paintable = None
        self.set_paintable(paintable)
        self.views = None        # per-frame zoom (x, y, w, h) inside the shown part
        self.fps = 60
        self.time_source = None  # () -> current recording time
        self.on_click = None
        # Finished-frame description (see set_scene)
        self.layout = None
        self.src_size = (16, 9)
        self.background = None   # Gdk.Texture
        self.overlays = []       # [(item, texture, (x, y, w, h), opacity, fade)]
        self.blurs = []          # doc blur items
        self.pointer = None      # {"frames", "rings", "texture", "pad", "px", "rings_on"}
        # Editing modes
        self.picking = False     # choosing a zoom spot
        self.on_pick = None
        self.spot = None
        self.amount = 1.8
        self._hover = None
        self.drawing = False     # drawing a blur area
        self.on_rect = None
        self.draw_rect = None    # current blur area while drawing, fractions of the recording
        self._drag_start = None

        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        motion_ctl = Gtk.EventControllerMotion()
        motion_ctl.connect("motion", self._moved)
        motion_ctl.connect("leave", lambda *_: self._moved(None, None, None))
        self.add_controller(motion_ctl)

    # ---- inputs ----------------------------------------------------------------

    def set_paintable(self, paintable):
        self.paintable = paintable
        paintable.connect("invalidate-contents", lambda *_: self.queue_draw())
        paintable.connect("invalidate-size", lambda *_: self.queue_draw())
        self.queue_draw()

    def set_views(self, views, fps):
        self.views, self.fps = views, fps
        self.queue_draw()

    def set_scene(self, layout, src_size, background, overlays, blurs):
        """layout: compose.layout(...); background: texture or None;
        overlays: [(item, texture, (x, y, w, h), opacity, fade)] in canvas pixels."""
        self.layout, self.src_size = layout, src_size
        self.background, self.overlays, self.blurs = background, overlays, blurs
        self.queue_draw()

    def set_pointer(self, pointer_data):
        self.pointer = pointer_data
        self.queue_draw()

    def set_picking(self, picking, spot=None, amount=None):
        self.picking = picking
        self.spot = spot
        if amount:
            self.amount = amount
        self._update_cursor()
        self.queue_draw()

    def set_drawing(self, drawing, rect=None):
        self.drawing = drawing
        self.draw_rect = rect
        self._update_cursor()
        self.queue_draw()

    def _update_cursor(self):
        editing = self.picking or self.drawing
        self.set_cursor(Gdk.Cursor.new_from_name("crosshair") if editing else None)

    # ---- geometry ---------------------------------------------------------------

    def _fit(self, ratio):
        width, height = self.get_width(), self.get_height()
        if width / max(1, height) > ratio:
            h = height
            w = h * ratio
        else:
            w = width
            h = w / ratio
        return (width - w) / 2, (height - h) / 2, w, h

    def _whole(self):
        """Where the whole recording sits when it is shown on its own."""
        ratio = self.paintable.get_intrinsic_aspect_ratio() or (self.src_size[0] / self.src_size[1])
        return self._fit(ratio)

    def _fraction(self, x, y):
        fx, fy, fw, fh = self._whole()
        return min(1, max(0, (x - fx) / fw)), min(1, max(0, (y - fy) / fh))

    def _time(self):
        return self.time_source() if self.time_source else 0.0

    def _view(self, t):
        if not self.views:
            return 0.0, 0.0, 1.0, 1.0
        i = int(round(t * self.fps))
        return self.views[max(0, min(len(self.views) - 1, i))]

    # ---- pointer -------------------------------------------------------------------

    def _clicked(self, _gesture, _n, x, y):
        if self.drawing:
            return
        if self.picking and self.on_pick:
            self.on_pick(*self._fraction(x, y))
        elif self.on_click:
            self.on_click()

    def _moved(self, _c, x, y):
        if not self.picking:
            return
        self._hover = None if x is None else self._fraction(x, y)
        self.queue_draw()

    def _drag_begin(self, _g, x, y):
        if self.drawing:
            self._drag_start = self._fraction(x, y)

    def _drag_update(self, gesture, dx, dy):
        if not (self.drawing and self._drag_start):
            return
        _ok, x0, y0 = gesture.get_start_point()
        a, b = self._drag_start, self._fraction(x0 + dx, y0 + dy)
        self.draw_rect = [min(a[0], b[0]), min(a[1], b[1]), abs(a[0] - b[0]), abs(a[1] - b[1])]
        self.queue_draw()

    def _drag_end(self, _g, _dx, _dy):
        if not (self.drawing and self._drag_start):
            return
        self._drag_start = None
        r = self.draw_rect
        if r and r[2] > 0.01 and r[3] > 0.01 and self.on_rect:
            self.on_rect(r)

    # ---- drawing --------------------------------------------------------------------

    def _draw_video(self, snap, box, view):
        """Draws the part `view` (fractions of the recording) into `box`."""
        bx, by, bw, bh = box
        ux, uy, uw, uh = view
        sw, sh = bw / uw, bh / uh
        point = Graphene.Point()
        point.init(bx - ux * sw, by - uy * sh)
        snap.save()
        snap.translate(point)
        self.paintable.snapshot(snap, sw, sh)
        snap.restore()

    def do_snapshot(self, snap):
        width, height = self.get_width(), self.get_height()
        snap.append_color(BLACK, _rect(0, 0, width, height))
        if self.picking or self.drawing or not self.layout:
            self._snapshot_whole(snap)
            return
        t = self._time()
        lay = self.layout
        W, H = lay["canvas"]
        fx, fy, fw, fh = self._fit(W / H)
        k = fw / W
        if self.background is not None:
            snap.append_texture(self.background, _rect(fx, fy, fw, fh))
        rx, ry, rw, rh = lay["rect"]
        box = (fx + rx * k, fy + ry * k, rw * k, rh * k)
        # What part of the recording shows: the shape's part, then the zoom inside it.
        sx, sy, sw, sh = lay["source"]
        vx, vy, vw, vh = self._view(t)
        view = (sx + vx * sw, sy + vy * sh, vw * sw, vh * sh)

        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(_rect(*box), lay["radius"] * k)
        snap.push_rounded_clip(rounded)
        self._draw_video(snap, box, view)
        # Blurs: the same part of the recording, drawn again blurred.
        src_w, src_h = self.src_size
        for b in self.blurs:
            if not (b["start"] <= t <= b["end"]):
                continue
            x, y, w, h = b["rect"]
            ex = box[0] + (x - view[0]) / view[2] * box[2]
            ey = box[1] + (y - view[1]) / view[3] * box[3]
            ew, eh = w / view[2] * box[2], h / view[3] * box[3]
            sigma = compose.blur_sigma(w * src_w, h * src_h, b["strength"])
            radius = sigma * box[2] / (view[2] * src_w)
            snap.push_clip(_rect(ex, ey, ew, eh))
            snap.push_blur(max(2.0, radius * 2))
            self._draw_video(snap, box, view)
            snap.pop()
            snap.pop()
        if self.pointer:
            self._draw_pointer(snap, t, box, view)
        snap.pop()

        # Captions and pictures, eased in and out.
        for item, texture, (ox, oy, ow, oh), opacity, fade in self.overlays:
            alpha = compose.fade_alpha(t, item["start"], item["end"], fade) * opacity
            if alpha <= 0.001:
                continue
            snap.push_opacity(alpha)
            snap.append_texture(texture, _rect(fx + ox * k, fy + oy * k, ow * k, oh * k))
            snap.pop()

    def _draw_pointer(self, snap, t, box, view):
        """The drawn pointer and click ring, in the recording's own space, so
        a zoom enlarges them together with everything else."""
        p = self.pointer
        frames = p["frames"]
        i = max(0, min(len(frames) - 1, int(round(t * self.fps))))
        src_w = self.src_size[0]
        k = box[2] / (view[2] * src_w)          # screen pixels per recording pixel

        def to_screen(fx, fy):
            return (box[0] + (fx - view[0]) / view[2] * box[2],
                    box[1] + (fy - view[1]) / view[3] * box[3])

        if p["rings_on"] and i < len(p["rings"]) and p["rings"][i] is not None:
            progress, rx, ry = p["rings"][i]
            radius, width, alpha = ptr.ring_shape(progress, p["px"])
            cx, cy = to_screen(rx, ry)
            r = max(0.5, (radius - width / 2) * k)
            rr = Gsk.RoundedRect()
            rr.init_from_rect(_rect(cx - r, cy - r, 2 * r, 2 * r), r)
            w = max(0.6, width * k)
            # A faint dark edge keeps the white ring visible on white pages.
            edge = max(1.0, w * 0.5)
            dark = Gsk.RoundedRect()
            dark.init_from_rect(_rect(cx - r - edge, cy - r - edge, 2 * (r + edge), 2 * (r + edge)), r + edge)
            snap.append_border(dark, [w + 2 * edge] * 4, [_rgba(0, 0, 0, alpha * 0.6)] * 4)
            snap.append_border(rr, [w] * 4, [_rgba(1, 1, 1, min(1, alpha * 1.6))] * 4)
        x, y, opacity, scale = frames[i]
        if opacity <= 0.005:
            return
        tex = p["texture"]
        sx, sy = to_screen(x, y)
        size = k * scale
        snap.push_opacity(opacity)
        snap.append_texture(tex, _rect(sx - p["pad"] * size, sy - p["pad"] * size,
                                       tex.get_width() * size, tex.get_height() * size))
        snap.pop()

    def _snapshot_whole(self, snap):
        fx, fy, fw, fh = self._whole()
        self._draw_video(snap, (fx, fy, fw, fh), (0.0, 0.0, 1.0, 1.0))
        if self.picking:
            frame = None
            if self._hover:
                frame = motion.spot_view(*self._hover, self.amount)
            elif self.spot:
                frame = motion.spot_view(*self.spot, self.amount)
            if frame:
                self._dim_outside(snap, frame, fx, fy, fw, fh)
            if self.spot:
                self._frame(snap, motion.spot_view(*self.spot, self.amount), fx, fy, fw, fh, CHOSEN, 3)
            if self._hover:
                self._frame(snap, motion.spot_view(*self._hover, self.amount), fx, fy, fw, fh, HOVER, 2)
        if self.drawing:
            if self.draw_rect:
                self._dim_outside(snap, self.draw_rect, fx, fy, fw, fh)
                x, y, w, h = self.draw_rect
                rr = Gsk.RoundedRect()
                rr.init_from_rect(_rect(fx + x * fw, fy + y * fh, w * fw, h * fh), 0)
                snap.append_border(rr, [2] * 4, [BLUR_EDGE] * 4)
            else:
                snap.append_color(DIM, _rect(fx, fy, fw, fh))

    @staticmethod
    def _dim_outside(snap, view, fx, fy, fw, fh):
        x, y, w, h = (fx + view[0] * fw, fy + view[1] * fh, view[2] * fw, view[3] * fh)
        snap.append_color(DIM, _rect(fx, fy, fw, y - fy))
        snap.append_color(DIM, _rect(fx, y + h, fw, fy + fh - y - h))
        snap.append_color(DIM, _rect(fx, y, x - fx, h))
        snap.append_color(DIM, _rect(x + w, y, fx + fw - x - w, h))

    @staticmethod
    def _frame(snap, view, fx, fy, fw, fh, color, width):
        x, y, w, h = view
        rx, ry, rw, rh = fx + x * fw, fy + y * fh, w * fw, h * fh
        cx, cy = rx + rw / 2, ry + rh / 2
        # A dark edge under the colour keeps the frame visible on any picture.
        for c, grow, wd in ((EDGE, 1, width + 2), (color, 0, width)):
            rr = Gsk.RoundedRect()
            rr.init_from_rect(_rect(rx - grow, ry - grow, rw + 2 * grow, rh + 2 * grow), 6)
            snap.append_border(rr, [wd] * 4, [c] * 4)
            snap.append_color(c, _rect(cx - 10 - grow, cy - 1 - grow, 20 + 2 * grow, 2 + 2 * grow))
            snap.append_color(c, _rect(cx - 1 - grow, cy - 10 - grow, 2 + 2 * grow, 20 + 2 * grow))
