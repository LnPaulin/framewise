"""Drawing a screenshot with its marks: shared by the editor view and saving.

Marks are kept in the ORIGINAL picture's pixels, so cropping or adding a
background never moves them. The picture is drawn in three passes:
areas that change the picture itself (blur, pixelate, solid
block), then the spotlight dimming, then the marks drawn on top.
"""

import math

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

from . import compose  # noqa: E402

COLORS = {
    "red": (0.898, 0.282, 0.302), "orange": (0.97, 0.42, 0.08), "yellow": (1.0, 0.77, 0.24),
    "green": (0.19, 0.64, 0.42), "blue": (0.0, 0.56, 1.0), "purple": (0.56, 0.31, 0.78),
    "white": (1.0, 1.0, 1.0), "black": (0.07, 0.07, 0.08),
}
WIDTHS = {"s": 3, "m": 6, "l": 10}
TEXT_SIZES = {"s": 22, "m": 32, "l": 48}
REGION_TYPES = ("pixelate", "blur", "redact")
BOX_TYPES = ("rect", "ellipse", "pixelate", "blur", "redact", "spotlight")
LINE_TYPES = ("arrow", "line")
PATH_TYPES = ("pen", "highlighter")
POINT_TYPES = ("text", "number")


def new_doc():
    return {"crop": None, "items": [],
            "bg": {"kind": "none", "preset": "ocean", "color": [0.12, 0.12, 0.14],
                   "padding": 0.5, "radius": 0.0, "shadow": True}}


def unit(img_w, img_h):
    """Mark sizes grow with big pictures so they look the same once shrunk to fit."""
    return max(1.0, min(img_w, img_h) / 900)


# ---- geometry -------------------------------------------------------------------------

def crop_rect(doc, img_w, img_h):
    c = doc.get("crop")
    if not c:
        return (0, 0, img_w, img_h)
    x, y, w, h = (int(round(v)) for v in c)
    x, y = max(0, min(img_w - 1, x)), max(0, min(img_h - 1, y))
    return (x, y, max(1, min(img_w - x, w)), max(1, min(img_h - y, h)))


def padding_px(cw, ch, bg):
    return round(max(24, min(cw, ch) * (0.03 + 0.12 * bg["padding"])))


def output_layout(doc, img_w, img_h, editing_crop=False):
    """(canvas_w, canvas_h, image_x, image_y, crop) of the finished picture.

    image_x/y: where the original picture's (0, 0) lands on the canvas.
    """
    if editing_crop:
        return img_w, img_h, 0, 0, (0, 0, img_w, img_h)
    cx, cy, cw, ch = crop_rect(doc, img_w, img_h)
    bg = doc["bg"]
    if bg["kind"] == "none":
        return cw, ch, -cx, -cy, (cx, cy, cw, ch)
    p = padding_px(cw, ch, bg)
    return cw + 2 * p, ch + 2 * p, p - cx, p - cy, (cx, cy, cw, ch)


def item_bounds(item):
    """(x, y, w, h) around a mark, in picture pixels."""
    t = item["type"]
    if t in BOX_TYPES:
        x, y, w, h = item["r"]
        return (min(x, x + w), min(y, y + h), abs(w), abs(h))
    if t in LINE_TYPES:
        x1, y1, x2, y2 = item["p"]
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    if t in PATH_TYPES:
        xs, ys = item["pts"][0::2], item["pts"][1::2]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    if t == "number":
        r = item["size"] * 0.75
        return (item["p"][0] - r, item["p"][1] - r, 2 * r, 2 * r)
    if t == "text":
        w, h = text_size(item)
        return (item["p"][0], item["p"][1], w, h)
    return (0, 0, 0, 0)


def norm_box(r):
    x, y, w, h = r
    return [min(x, x + w), min(y, y + h), abs(w), abs(h)]


def move_item(item, dx, dy):
    t = item["type"]
    if t in BOX_TYPES:
        item["r"] = [item["r"][0] + dx, item["r"][1] + dy, item["r"][2], item["r"][3]]
    elif t in LINE_TYPES:
        x1, y1, x2, y2 = item["p"]
        item["p"] = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
    elif t in PATH_TYPES:
        pts = item["pts"]
        item["pts"] = [v + (dx if i % 2 == 0 else dy) for i, v in enumerate(pts)]
    elif t in POINT_TYPES:
        item["p"] = [item["p"][0] + dx, item["p"][1] + dy]


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def hit(item, x, y, tol):
    t = item["type"]
    if t in LINE_TYPES:
        return _seg_dist(x, y, *item["p"]) <= tol + item["width"]
    if t in PATH_TYPES:
        pts = item["pts"]
        w = item["width"] * (3 if t == "highlighter" else 1)
        for i in range(0, len(pts) - 2, 2):
            if _seg_dist(x, y, pts[i], pts[i + 1], pts[i + 2], pts[i + 3]) <= tol + w:
                return True
        return len(pts) == 2 and math.hypot(x - pts[0], y - pts[1]) <= tol + w
    bx, by, bw, bh = item_bounds(item)
    if t in ("rect", "ellipse") and not item.get("fill"):
        # Only the outline counts, so marks inside a box stay clickable.
        inside = bx - tol <= x <= bx + bw + tol and by - tol <= y <= by + bh + tol
        core = bx + tol < x < bx + bw - tol and by + tol < y < by + bh - tol
        return inside and not core
    return bx - tol <= x <= bx + bw + tol and by - tol <= y <= by + bh + tol


def handles(item):
    """Points that resize the mark: [(name, x, y)]."""
    t = item["type"]
    if t in BOX_TYPES:
        x, y, w, h = norm_box(item["r"])
        return [("tl", x, y), ("tr", x + w, y), ("bl", x, y + h), ("br", x + w, y + h)]
    if t in LINE_TYPES:
        x1, y1, x2, y2 = item["p"]
        return [("p1", x1, y1), ("p2", x2, y2)]
    return []


def drag_handle(item, name, x, y):
    t = item["type"]
    if t in LINE_TYPES:
        p = list(item["p"])
        if name == "p1":
            p[0], p[1] = x, y
        else:
            p[2], p[3] = x, y
        item["p"] = p
        return
    bx, by, bw, bh = norm_box(item["r"])
    x0, y0, x1, y1 = bx, by, bx + bw, by + bh
    if "l" in name:
        x0 = x
    else:
        x1 = x
    if "t" in name:
        y0 = y
    else:
        y1 = y
    item["r"] = norm_box([x0, y0, x1 - x0, y1 - y0])


# ---- text ------------------------------------------------------------------------------

_measure = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


def _layout(cr, item):
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string("Sans Bold")
    font.set_absolute_size(item["size"] * Pango.SCALE)
    layout.set_font_description(font)
    layout.set_text(item["text"] or " ", -1)
    return layout


def text_size(item):
    _ink, logical = _layout(_measure, item).get_pixel_extents()
    return logical.width + item["size"] * 0.2, logical.height


# ---- rendering -------------------------------------------------------------------------

def flatten(base, items):
    """A copy of the picture with the blurred, pixelated and blocked areas applied."""
    w, h = base.get_width(), base.get_height()
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(out)
    cr.set_source_surface(base, 0, 0)
    cr.paint()
    for item in items:
        if item["type"] not in REGION_TYPES:
            continue
        x, y, rw, rh = (int(round(v)) for v in norm_box(item["r"]))
        x, y = max(0, x), max(0, y)
        rw, rh = min(rw, w - x), min(rh, h - y)
        if rw < 2 or rh < 2:
            continue
        cr.save()
        cr.rectangle(x, y, rw, rh)
        cr.clip()
        if item["type"] == "redact":
            cr.set_source_rgb(*COLORS.get(item["color"], COLORS["black"]))
            cr.paint()
        else:
            strength = item.get("strength", 3)
            if item["type"] == "pixelate":
                block = max(4, round(min(rw, rh) / (22 - strength * 3)))
                _scaled_copy(cr, out, x, y, rw, rh, block, cairo.FILTER_NEAREST)
            else:
                # A soft blur: shrink a lot, then grow back smoothly, twice.
                block = max(3, round(min(rw, rh) / (26 - strength * 4)))
                for _ in range(2):
                    _scaled_copy(cr, out, x, y, rw, rh, block, cairo.FILTER_BILINEAR)
        cr.restore()
    out.flush()
    return out


def _scaled_copy(cr, src, x, y, w, h, block, filter_up):
    sw, sh = max(1, math.ceil(w / block)), max(1, math.ceil(h / block))
    small = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
    sc = cairo.Context(small)
    sc.scale(sw / w, sh / h)
    sc.set_source_surface(src, -x, -y)
    sc.get_source().set_filter(cairo.FILTER_GOOD)
    sc.paint()
    small.flush()
    cr.save()
    cr.translate(x, y)
    cr.scale(w / sw, h / sh)
    cr.set_source_surface(small, 0, 0)
    pat = cr.get_source()
    pat.set_filter(filter_up)
    pat.set_extend(cairo.EXTEND_PAD)
    cr.paint()
    cr.restore()


def backdrop_radius(doc, img_w, img_h, editing_crop=False):
    """Corner radius of the picture, with or without a background (without
    one, the saved PNG has transparent corners)."""
    if editing_crop:
        return 0.0
    _cx, _cy, cw, ch = crop_rect(doc, img_w, img_h)
    return min(cw, ch) * 0.06 * doc["bg"]["radius"]


def paint_backdrop(cr, doc, img_w, img_h):
    """The background and the shadow under the picture, in canvas pixels."""
    W, H, ox, oy, (cx, cy, cw, ch) = output_layout(doc, img_w, img_h)
    bg = doc["bg"]
    if bg["kind"] == "none":
        return
    _paint_background(cr, W, H, bg)
    if bg.get("shadow"):
        _paint_shadow(cr, ox + cx, oy + cy, cw, ch, backdrop_radius(doc, img_w, img_h), min(cw, ch))


def has_marks(doc, skip=None):
    return any(it is not skip and it["type"] not in REGION_TYPES for it in doc["items"])


def paint_marks(cr, doc, W, H, ox, oy, skip=None):
    """Spotlight dimming, then every mark. cr is in picture pixels."""
    spots = [it for it in doc["items"] if it["type"] == "spotlight"]
    if spots:
        cr.save()
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(-ox, -oy, W, H)
        for it in spots:
            x, y, w, h = norm_box(it["r"])
            _rounded(cr, x, y, w, h, min(w, h) * 0.08)
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.fill()
        cr.restore()
    for item in doc["items"]:
        if item is not skip and item["type"] not in REGION_TYPES + ("spotlight",):
            draw_item(cr, item)


def render(cr, doc, flat, img_w, img_h, editing_crop=False, skip=None):
    """Draws the finished picture at full size. flat: flatten() output."""
    W, H, ox, oy, (cx, cy, cw, ch) = output_layout(doc, img_w, img_h, editing_crop)
    cr.save()
    if not editing_crop:
        paint_backdrop(cr, doc, img_w, img_h)
    # The picture and its marks, clipped to the crop (rounded on a background).
    _rounded(cr, ox + cx, oy + cy, cw, ch, backdrop_radius(doc, img_w, img_h, editing_crop))
    cr.clip()
    cr.set_source_surface(flat, ox, oy)
    cr.paint()
    cr.translate(ox, oy)
    paint_marks(cr, doc, W, H, ox, oy, skip)
    cr.restore()


def draw_item(cr, item):
    t = item["type"]
    color = COLORS.get(item.get("color"), COLORS["red"])
    cr.save()
    cr.new_path()   # text leaves a current point that would join the next shape
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if t == "arrow":
        _arrow(cr, *item["p"], item["width"], color)
    elif t == "line":
        cr.set_source_rgb(*color)
        cr.set_line_width(item["width"])
        cr.move_to(*item["p"][:2])
        cr.line_to(*item["p"][2:])
        cr.stroke()
    elif t in ("rect", "ellipse"):
        x, y, w, h = norm_box(item["r"])
        if t == "rect":
            _rounded(cr, x, y, w, h, min(item["width"] * 1.5, w / 2, h / 2))
        elif w > 0 and h > 0:
            cr.save()
            cr.translate(x + w / 2, y + h / 2)
            cr.scale(w / 2, h / 2)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
        cr.set_source_rgb(*color)
        if item.get("fill"):
            cr.fill()
        else:
            cr.set_line_width(item["width"])
            cr.stroke()
    elif t in PATH_TYPES:
        pts = item["pts"]
        cr.move_to(pts[0], pts[1])
        for i in range(2, len(pts), 2):
            cr.line_to(pts[i], pts[i + 1])
        if len(pts) == 2:
            cr.line_to(pts[0] + 0.01, pts[1])
        if t == "highlighter":
            cr.set_operator(cairo.OPERATOR_MULTIPLY)
            cr.set_source_rgba(*color, 0.45)
            cr.set_line_width(item["width"] * 3)
            cr.set_line_cap(cairo.LINE_CAP_SQUARE)
        else:
            cr.set_source_rgb(*color)
            cr.set_line_width(item["width"])
        cr.stroke()
    elif t == "number":
        x, y = item["p"]
        r = item["size"] * 0.75
        cr.arc(x, y, r, 0, 2 * math.pi)
        cr.set_source_rgb(*color)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.set_line_width(max(1.5, r * 0.08))
        cr.stroke()
        layout = _layout(cr, {"text": str(item["n"]), "size": item["size"] * 0.8})
        _ink, logical = layout.get_pixel_extents()
        cr.move_to(x - logical.width / 2, y - logical.height / 2)
        light = color == COLORS["white"] or color == COLORS["yellow"]
        cr.set_source_rgb(*((0.07, 0.07, 0.08) if light else (1, 1, 1)))
        PangoCairo.show_layout(cr, layout)
        cr.new_path()
    elif t == "text":
        layout = _layout(cr, item)
        cr.move_to(*item["p"])
        PangoCairo.layout_path(cr, layout)
        # A thin outline in the opposite tone keeps text readable anywhere.
        dark = sum(color) < 1.5
        cr.set_source_rgba(*((1, 1, 1) if dark else (0, 0, 0)), 0.85)
        cr.set_line_width(max(2.0, item["size"] * 0.12))
        cr.stroke_preserve()
        cr.set_source_rgb(*color)
        cr.fill()
    cr.new_path()
    cr.restore()


def _arrow(cr, x1, y1, x2, y2, width, color):
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 1:
        return
    head = min(length * 0.6, max(14, width * 4.2))
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    bx, by = x2 - ux * head, y2 - uy * head
    spread = head * 0.55
    cr.set_source_rgb(*color)
    cr.set_line_width(width)
    cr.move_to(x1, y1)
    cr.line_to(bx + ux * 1, by + uy * 1)
    cr.stroke()
    cr.move_to(x2, y2)
    cr.line_to(bx - uy * spread, by + ux * spread)
    cr.line_to(bx + uy * spread, by - ux * spread)
    cr.close_path()
    cr.fill()


def _paint_background(cr, W, H, bg):
    if bg["kind"] == "preset":
        a, b = compose.PRESETS.get(bg.get("preset"), compose.PRESETS["ocean"])
        grad = cairo.LinearGradient(0, 0, W, H)
        grad.add_color_stop_rgb(0, *a)
        grad.add_color_stop_rgb(1, *b)
        cr.set_source(grad)
    else:
        cr.set_source_rgb(*bg.get("color", [0.12, 0.12, 0.14]))
    cr.rectangle(0, 0, W, H)
    cr.fill()


def _paint_shadow(cr, x, y, w, h, radius, short):
    # Layers of faint rounded boxes stand in for a blurred shadow.
    spread = max(6, short * 0.025)
    steps = 10
    for i in range(steps, 0, -1):
        grow = spread * i / steps
        _rounded(cr, x - grow, y - grow + spread * 0.35, w + 2 * grow, h + 2 * grow, radius + grow)
        cr.set_source_rgba(0, 0, 0, 0.035)
        cr.fill()


def _rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r < 0.5:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def export_png(base, doc, path):
    """Writes the finished picture at full size."""
    img_w, img_h = base.get_width(), base.get_height()
    W, H, *_ = output_layout(doc, img_w, img_h)
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(out)
    render(cr, doc, flatten(base, doc["items"]), img_w, img_h)
    out.write_to_png(str(path))
    return W, H
