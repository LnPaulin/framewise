"""Choosing what a screenshot keeps, on a frozen picture of every screen.

Each screen gets a full-screen layer showing its part of the still. Drag to
keep an area, click a window to keep that window, click the desktop or press
Enter to keep the whole screen, Esc to cancel. The window under the pointer
is highlighted in blue, and a magnifier helps hit exact pixels (Z hides it).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gsk, Gtk  # noqa: E402

from .state import settings  # noqa: E402

DIM = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.45)
BLUE = Gdk.RGBA(red=0.21, green=0.52, blue=0.89, alpha=1.0)
BLUE_TINT = Gdk.RGBA(red=0.21, green=0.52, blue=0.89, alpha=0.18)
WHITE = Gdk.RGBA(red=1, green=1, blue=1, alpha=1)
BADGE_BG = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.78)
CLICK_SLOP = 4        # pixels a click may move and still count as a click
MIN_SIDE = 8
LOUPE = 128           # magnifier size, logical pixels
LOUPE_ZOOM = 8


HINT = ("Drag for an area  ·  Click a window  ·  Enter: whole screen  ·  "
        "R: last area  ·  Z: magnifier  ·  Esc: cancel")
TEXT_HINT = "Drag over the text to copy  ·  Click a window  ·  R: last area  ·  Esc: cancel"


def _rect(x, y, w, h):
    r = Graphene.Rect()
    r.init(x, y, w, h)
    return r


def monitors():
    """[(Gdk.Monitor, (x, y, w, h))] in desktop (logical) coordinates."""
    out = []
    ms = Gdk.Display.get_default().get_monitors()
    for i in range(ms.get_n_items()):
        m = ms.get_item(i)
        g = m.get_geometry()
        out.append((m, (g.x, g.y, g.width, g.height)))
    return out


def desktop_bounds():
    rects = [r for _m, r in monitors()]
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


def window_at(windows, x, y):
    """The top-most window containing the point, or None."""
    for w in windows:
        if w["x"] <= x < w["x"] + w["w"] and w["y"] <= y < w["y"] + w["h"]:
            return w
    return None


def drag_rect(start, end):
    (x0, y0), (x1, y1) = start, end
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)


class ShotSelector:
    """on_done(result): ("area", rect) / ("window", window) / ("screen", rect) / None.

    Rectangles are in desktop coordinates (logical pixels).
    """

    def __init__(self, app, still_path, windows, on_done, hint=None):
        self.on_done = on_done
        self.hint = hint or HINT
        self.windows = windows
        self.finished = False
        self.pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(still_path))
        bx, by, bw, bh = desktop_bounds()
        self.origin = (bx, by)
        # Screenshot pixels per desktop pixel (more than 1 on scaled screens).
        self.ratio = self.pixbuf.get_width() / bw
        self.pointer = None          # desktop coordinates
        self.start = None
        self.rect = None             # desktop coordinates, while dragging
        self.loupe = settings.get("shot_loupe")
        self.layers = []
        for monitor, geo in monitors():
            self.layers.append(_Layer(self, app, monitor, geo))

    # ---- shared state ------------------------------------------------------------------

    def texture_for(self, geo):
        x, y, w, h = geo
        k = self.ratio
        px, py = round((x - self.origin[0]) * k), round((y - self.origin[1]) * k)
        pw = min(round(w * k), self.pixbuf.get_width() - px)
        ph = min(round(h * k), self.pixbuf.get_height() - py)
        sub = self.pixbuf.new_subpixbuf(px, py, pw, ph)
        return Gdk.Texture.new_for_pixbuf(sub)

    def redraw(self):
        for layer in self.layers:
            layer.canvas.queue_draw()

    def hovered_window(self):
        if self.pointer is None or self.start is not None:
            return None
        return window_at(self.windows, *self.pointer)

    def screen_at(self, point):
        for layer in self.layers:
            x, y, w, h = layer.geo
            if point and x <= point[0] < x + w and y <= point[1] < y + h:
                return layer.geo
        return self.layers[0].geo

    def finish(self, result):
        if self.finished:
            return
        self.finished = True
        for layer in self.layers:
            layer.win.destroy()
        # Let the layers disappear from the screen before anything is captured.
        GLib.timeout_add(150, lambda: (self.on_done(result), False)[1])

    # ---- input -------------------------------------------------------------------------

    def press(self, point):
        self.start = point
        self.rect = None

    def drag(self, point):
        if self.start is None:
            return
        self.pointer = point
        self.rect = drag_rect(self.start, point)
        self.redraw()

    def release(self, point):
        if self.start is None:
            return
        start, self.start = self.start, None
        x, y, w, h = drag_rect(start, point)
        if w <= CLICK_SLOP and h <= CLICK_SLOP:
            win = window_at(self.windows, *point)
            self.finish(("window", win) if win else ("screen", self.screen_at(point)))
            return
        if w < MIN_SIDE or h < MIN_SIDE:
            self.rect = None
            self.redraw()
            return
        settings.set("shot_last_area", [x, y, w, h])
        self.finish(("area", (x, y, w, h)))

    def key(self, keyval):
        if keyval == Gdk.KEY_Escape:
            self.finish(None)
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.finish(("screen", self.screen_at(self.pointer)))
        elif keyval in (Gdk.KEY_r, Gdk.KEY_R):
            last = settings.get("shot_last_area")
            if last:
                self.finish(("area", tuple(last)))
        elif keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self.loupe = not self.loupe
            settings.set("shot_loupe", self.loupe)
            self.redraw()
        else:
            return False
        return True


class _Layer:
    def __init__(self, selector, app, monitor, geo):
        self.sel = selector
        self.geo = geo
        self.texture = selector.texture_for(geo)
        self.win = Gtk.Window(application=app, decorated=False, title="framewise screenshot")
        self.win.add_css_class("ck-overlay")
        self.win.fullscreen_on_monitor(monitor)
        self.canvas = _Canvas(self)
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
        self.win.set_child(self.canvas)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self._moved(x, y))
        self.canvas.add_controller(motion)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda _g, x, y: selector.press(self.to_desktop(x, y)))
        drag.connect("drag-update", self._drag)
        drag.connect("drag-end", self._drag_end)
        self.canvas.add_controller(drag)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, _code, _st: selector.key(kv))
        self.win.add_controller(keys)
        self.win.present()

    def to_desktop(self, x, y):
        return (self.geo[0] + x, self.geo[1] + y)

    def to_local(self, x, y):
        return (x - self.geo[0], y - self.geo[1])

    def _moved(self, x, y):
        self.sel.pointer = self.to_desktop(x, y)
        self.sel.redraw()

    def _drag(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        self.sel.drag(self.to_desktop(sx + dx, sy + dy))

    def _drag_end(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        self.sel.release(self.to_desktop(sx + dx, sy + dy))


class _Canvas(Gtk.Widget):
    def __init__(self, layer):
        super().__init__(hexpand=True, vexpand=True)
        self.layer = layer

    def do_snapshot(self, snap):
        layer, sel = self.layer, self.layer.sel
        W, H = self.get_width(), self.get_height()
        snap.append_texture(layer.texture, _rect(0, 0, W, H))
        if sel.rect:
            x, y = layer.to_local(*sel.rect[:2])
            w, h = sel.rect[2], sel.rect[3]
            snap.append_color(DIM, _rect(0, 0, W, max(0, y)))
            snap.append_color(DIM, _rect(0, y + h, W, max(0, H - y - h)))
            snap.append_color(DIM, _rect(0, y, max(0, x), h))
            snap.append_color(DIM, _rect(x + w, y, max(0, W - x - w), h))
            outline = Gsk.RoundedRect()
            outline.init_from_rect(_rect(x - 1, y - 1, w + 2, h + 2), 0)
            snap.append_border(outline, [1] * 4, [WHITE] * 4)
            k = sel.ratio
            self._badge(snap, f"{round(w * k)} × {round(h * k)}", x, y + h + 10, W, H)
        else:
            snap.append_color(DIM, _rect(0, 0, W, H))
            win = sel.hovered_window()
            if win:
                x, y = layer.to_local(win["x"], win["y"])
                r = _rect(x, y, win["w"], win["h"])
                snap.push_clip(r)
                snap.append_texture(layer.texture, _rect(0, 0, W, H))
                snap.append_color(BLUE_TINT, _rect(0, 0, W, H))
                snap.pop()
                outline = Gsk.RoundedRect()
                outline.init_from_rect(r, 0)
                snap.append_border(outline, [3] * 4, [BLUE] * 4)
            pointer_here = sel.pointer and sel.screen_at(sel.pointer) == layer.geo
            if pointer_here or sel.pointer is None:
                self._badge(snap, sel.hint, W / 2, 28, W, H, center=True)
        if sel.loupe and sel.pointer and sel.screen_at(sel.pointer) == layer.geo:
            self._loupe(snap, *layer.to_local(*sel.pointer), W, H)

    def _loupe(self, snap, px, py, W, H):
        size, zoom = LOUPE, LOUPE_ZOOM
        lx = px + 24 if px + 24 + size < W else px - 24 - size
        ly = py + 24 if py + 24 + size + 30 < H else py - 24 - size - 30
        box = Gsk.RoundedRect()
        box.init_from_rect(_rect(lx, ly, size, size), size / 2)
        snap.push_rounded_clip(box)
        # The whole screen, magnified so the pointer's pixel sits in the middle.
        snap.append_scaled_texture(
            self.layer.texture, Gsk.ScalingFilter.NEAREST,
            _rect(lx + size / 2 - px * zoom, ly + size / 2 - py * zoom, W * zoom, H * zoom))
        c = size / 2
        snap.append_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=0.9), _rect(lx + c - zoom / 2, ly, 1, size))
        snap.append_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=0.9), _rect(lx, ly + c - zoom / 2, size, 1))
        snap.pop()
        snap.append_border(box, [2] * 4, [WHITE] * 4)
        k = self.layer.sel.ratio
        gx, gy = self.layer.sel.pointer
        self._badge(snap, f"{round((gx - self.layer.sel.origin[0]) * k)}, "
                          f"{round((gy - self.layer.sel.origin[1]) * k)}",
                    lx + size / 2, ly + size + 18, W, H, center=True)

    def _badge(self, snap, text, x, y, width, height, center=False):
        layout = self.create_pango_layout(None)
        layout.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>", -1)
        _ink, logical = layout.get_pixel_extents()
        pad_x, pad_y = 10, 6
        bw, bh = logical.width + 2 * pad_x, logical.height + 2 * pad_y
        if center:
            x, y = x - bw / 2, y - bh / 2
        x = min(max(6, x), width - bw - 6)
        y = min(max(6, y), height - bh - 6)
        bg = Gsk.RoundedRect()
        bg.init_from_rect(_rect(x, y, bw, bh), 8)
        snap.push_rounded_clip(bg)
        snap.append_color(BADGE_BG, _rect(x, y, bw, bh))
        snap.pop()
        point = Graphene.Point()
        point.init(x + pad_x, y + pad_y)
        snap.save()
        snap.translate(point)
        snap.append_layout(layout, WHITE)
        snap.restore()
