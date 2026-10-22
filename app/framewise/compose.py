"""How the finished picture is put together: background, shape, overlays.

Shared by the editor preview and the export, so both draw the same thing.
Rules:

- With a background, the canvas grows around the recording (a margin never
  costs sharpness) and is then widened to the chosen shape, capped at 3840
  pixels on the long side.
- Without one, a shape crops the largest centred frame from the recording.
- Captions and pictures sit at one of nine places on the canvas, so they
  stay put whatever shape the video ends up with.
- A blur covers a part of the RECORDING (before zoom), so it hides the same
  pixels for as long as it lasts, even inside a zoom.
"""

import math
from pathlib import Path

MAX_EDGE = 3840
ASPECTS = {"original": None, "wide": 16 / 9, "square": 1.0, "vertical": 9 / 16}

# Gradient presets (top-left to bottom-right).
PRESETS = {
    "ocean": [(0.20, 0.47, 0.96), (0.45, 0.83, 0.98)],
    "sunset": [(0.99, 0.36, 0.42), (1.00, 0.75, 0.35)],
    "forest": [(0.07, 0.56, 0.43), (0.62, 0.87, 0.50)],
    "candy": [(0.66, 0.32, 0.95), (0.99, 0.56, 0.65)],
    "graphite": [(0.23, 0.25, 0.31), (0.55, 0.60, 0.70)],
}

ANCHORS = ["topLeading", "top", "topTrailing", "leading", "center", "trailing",
           "bottomLeading", "bottom", "bottomTrailing"]
TEXT_COLORS = {"white": (1, 1, 1), "black": (0, 0, 0), "accent": (0.36, 0.55, 1.0),
               "yellow": (1.0, 0.84, 0.04), "red": (0.9, 0.28, 0.3), "green": (0.2, 0.78, 0.35)}

TEXT_FADE = 0.25
IMAGE_FADE = 0.25


def default_style():
    return {"background": {"kind": "none", "preset": "graphite", "color": [0.12, 0.12, 0.14],
                           "image": None, "padding": 0.45, "radius": 0.35, "blur": 0.0},
            "aspect": "original"}


def even(v):
    return max(2, int(round(v)) & ~1)


def padding_px(w, h, factor):
    """Margin around the recording: even 0 keeps a small frame."""
    f = max(0.0, min(1.0, factor))
    return max(24.0, min(w, h) * (0.035 + 0.14 * f))


def radius_px(w, h, factor):
    """Corner rounding: 0 square, 1 a generous fifth of the short side."""
    return min(w, h) * 0.2 * max(0.0, min(1.0, factor))


def layout(src_w, src_h, style):
    """Where things go, in output pixels.

    src_w, src_h: the recorded picture (after the area crop).
    Returns dict:
      canvas (W, H)         size of the finished video
      rect (x, y, w, h)     where the recording is drawn on the canvas
      radius                its corner rounding in pixels
      source (x, y, w, h)   the part of the recording that is shown, as
                            fractions (a shape without a background crops it)
      background            True when a background is drawn
    """
    bg = style["background"]
    ratio = ASPECTS.get(style.get("aspect", "original"))
    has_bg = bg.get("kind", "none") != "none"
    source = (0.0, 0.0, 1.0, 1.0)
    if not has_bg:
        if ratio is None:
            return {"canvas": (even(src_w), even(src_h)), "rect": (0, 0, even(src_w), even(src_h)),
                    "radius": 0.0, "source": source, "background": False}
        # The largest centred frame of that shape.
        if src_w / src_h > ratio:
            w, h = src_h * ratio, src_h
        else:
            w, h = src_w, src_w / ratio
        source = ((src_w - w) / 2 / src_w, (src_h - h) / 2 / src_h, w / src_w, h / src_h)
        scale = min(1.0, MAX_EDGE / max(w, h))
        cw, ch = even(w * scale), even(h * scale)
        return {"canvas": (cw, ch), "rect": (0, 0, cw, ch), "radius": 0.0,
                "source": source, "background": False}

    pad = padding_px(src_w, src_h, bg.get("padding", 0.45))
    cw, ch = src_w + 2 * pad, src_h + 2 * pad
    if ratio is not None:
        if cw / ch < ratio:
            cw = ch * ratio
        else:
            ch = cw / ratio
    # The recording fits inside the canvas minus the margin, centred.
    avail_w, avail_h = cw - 2 * pad, ch - 2 * pad
    k = min(avail_w / src_w, avail_h / src_h)
    rw, rh = src_w * k, src_h * k
    scale = min(1.0, MAX_EDGE / max(cw, ch))
    W, H = even(cw * scale), even(ch * scale)
    rw, rh = even(rw * scale), even(rh * scale)
    x, y = even((W - rw) / 2), even((H - rh) / 2)
    return {"canvas": (W, H), "rect": (x, y, rw, rh),
            "radius": radius_px(rw, rh, bg.get("radius", 0.35)),
            "source": source, "background": True}


def anchor_point(anchor, canvas, size, margin_frac=0.04):
    """Top-left position of something of `size` at one of the nine places."""
    W, H = canvas
    w, h = size
    m = min(W, H) * margin_frac
    col = 0 if "eading" in anchor else 2 if "railing" in anchor else 1
    row = 0 if anchor.startswith("top") else 2 if anchor.startswith("bottom") else 1
    x = [m, (W - w) / 2, W - w - m][col]
    y = [m, (H - h) / 2, H - h - m][row]
    return x, y


def fade_alpha(t, start, end, fade):
    """Opacity at t for something shown from start to end, eased at both ends."""
    if t < start or t > end:
        return 0.0
    ramp = min(fade, (end - start) / 2)
    if ramp <= 0:
        return 1.0
    if t < start + ramp:
        x = (t - start) / ramp
    elif t > end - ramp:
        x = (end - t) / ramp
    else:
        return 1.0
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def blur_sigma(rect_px_w, rect_px_h, strength):
    """Blur strong enough that text inside is unreadable (strength 1-5)."""
    side = max(8.0, min(rect_px_w, rect_px_h))
    return max(2.0, side / 6 * (0.5 + 0.25 * max(1, min(5, strength))))


# ---- images rendered once, used by both preview and export ---------------------------

def render_background(path, W, H, bg):
    """Writes the background (gradient, colour or picture) as a PNG of W x H."""
    import cairo
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
    cr = cairo.Context(surface)
    kind = bg.get("kind")
    if kind == "preset":
        stops = PRESETS.get(bg.get("preset"), PRESETS["graphite"])
        grad = cairo.LinearGradient(0, 0, W, H)
        grad.add_color_stop_rgb(0, *stops[0])
        grad.add_color_stop_rgb(1, *stops[1])
        cr.set_source(grad)
        cr.paint()
    elif kind == "image" and bg.get("image") and Path(bg["image"]).exists():
        _paint_image_cover(cr, bg["image"], W, H)
    else:
        cr.set_source_rgb(*bg.get("color", [0.12, 0.12, 0.14]))
        cr.paint()
    surface.write_to_png(str(path))
    if kind == "image" and bg.get("blur", 0) > 0:
        _blur_png(path, bg["blur"] * min(W, H) / 40)


def _paint_image_cover(cr, image_path, W, H):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf
    pix = GdkPixbuf.Pixbuf.new_from_file(str(image_path))
    k = max(W / pix.get_width(), H / pix.get_height())
    scaled = pix.scale_simple(max(1, math.ceil(pix.get_width() * k)),
                              max(1, math.ceil(pix.get_height() * k)), GdkPixbuf.InterpType.BILINEAR)
    Gdk.cairo_set_source_pixbuf(cr, scaled, (W - scaled.get_width()) / 2, (H - scaled.get_height()) / 2)
    cr.paint()


def _blur_png(path, sigma):
    import subprocess
    tmp = Path(path).with_suffix(".blur.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf", f"gblur=sigma={sigma:.1f}",
                    str(tmp)], check=False)
    if tmp.exists():
        tmp.replace(path)


def render_mask(path, w, h, radius):
    """A white rounded rectangle on black: the recording's corner shape."""
    import cairo
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0, 0, 0)
    cr.paint()
    cr.set_source_rgb(1, 1, 1)
    _rounded(cr, 0, 0, w, h, radius)
    cr.fill()
    surface.write_to_png(str(path))


def render_text(path, text, height_px, color):
    """A caption as a transparent PNG: bold, with a soft shadow so it reads
    over any picture. Returns its (width, height)."""
    import cairo
    import gi
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo
    font_px = max(8, int(height_px))
    probe = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    layout_ = PangoCairo.create_layout(cairo.Context(probe))
    desc = Pango.FontDescription.from_string("Sans Bold")
    desc.set_absolute_size(font_px * Pango.SCALE)
    layout_.set_font_description(desc)
    layout_.set_alignment(Pango.Alignment.CENTER)
    layout_.set_text(text or " ", -1)
    _ink, logical = layout_.get_pixel_extents()
    pad = int(font_px * 0.35)
    w, h = max(2, logical.width + 2 * pad), max(2, logical.height + 2 * pad)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    layout_ = PangoCairo.create_layout(cr)
    layout_.set_font_description(desc)
    layout_.set_alignment(Pango.Alignment.CENTER)
    layout_.set_text(text or " ", -1)
    dark = sum(color) / 3 < 0.5
    shadow = (1, 1, 1, 0.55) if dark else (0, 0, 0, 0.6)
    for dx, dy in ((0, 2), (1, 1), (-1, 1), (0, 1)):
        cr.move_to(pad - logical.x + dx, pad - logical.y + dy)
        cr.set_source_rgba(*shadow)
        PangoCairo.show_layout(cr, layout_)
    cr.move_to(pad - logical.x, pad - logical.y)
    cr.set_source_rgb(*color)
    PangoCairo.show_layout(cr, layout_)
    surface.write_to_png(str(path))
    return w, h


def render_image(path, source, width_px, max_h):
    """An overlay picture scaled to width_px (never taller than max_h). Returns size."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    pix = GdkPixbuf.Pixbuf.new_from_file(str(source))
    k = min(width_px / pix.get_width(), max_h / pix.get_height())
    w, h = max(2, int(pix.get_width() * k)), max(2, int(pix.get_height() * k))
    pix.scale_simple(w, h, GdkPixbuf.InterpType.HYPER).savev(str(path), "png", [], [])
    return w, h


def _rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
