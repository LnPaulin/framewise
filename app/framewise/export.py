"""Turns a finished take into the MP4 the user keeps.

- Whole screen or window: the video is copied as is (no re-encoding, no loss).
- Area: the selected rectangle is cut out on the Intel GPU and re-encoded at
  a very high quality, which takes about a second per 8 seconds of 60 fps video.
- Sound: system sound and microphone are mixed into one AAC track, since most
  players only play the first track of an MP4.

The file is written next to its destination under a hidden name and only
renamed into place once it is complete, so a failed save never leaves a
broken file and never replaces an existing one.
"""

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from gi.repository import Gio, GLib

from . import gpu
from .state import DATA_DIR

TAKES_DIR = DATA_DIR / "takes"
ORPHAN_MAX_AGE = 24 * 3600


def new_take_dir():
    TAKES_DIR.mkdir(parents=True, exist_ok=True)
    name = "Take-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    path = TAKES_DIR / name
    n = 2
    while path.exists():
        path = TAKES_DIR / f"{name}-{n}"
        n += 1
    path.mkdir()
    return path


def sweep_old_takes():
    """Removes take folders older than a day (left behind by a crash or a quit)."""
    if not TAKES_DIR.exists():
        return
    cutoff = time.time() - ORPHAN_MAX_AGE
    for d in TAKES_DIR.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
        except OSError:
            pass


def write_take_info(take_dir, info):
    (Path(take_dir) / "take.json").write_text(json.dumps(info, indent=2))


def destination(folder):
    folder = Path(folder).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path.home() / "Videos"
        folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d at %H.%M.%S")
    path = folder / f"Recording {stamp}.mp4"
    n = 2
    while path.exists():
        path = folder / f"Recording {stamp} ({n}).mp4"
        n += 1
    return path


def video_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    w, h = out.stdout.strip().split(",")[:2]
    return int(w), int(h)


def crop_pixels(fraction, size):
    """Turns a 0-1 rectangle into even pixel values inside the picture."""
    fx, fy, fw, fh = fraction
    vw, vh = size
    x = max(0, int(round(fx * vw))) & ~1
    y = max(0, int(round(fy * vh))) & ~1
    w = min(vw - x, int(round(fw * vw))) & ~1
    h = min(vh - y, int(round(fh * vh))) & ~1
    return x, y, max(32, w), max(32, h)


def audio_track_count(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return len([line for line in out.stdout.splitlines() if line.strip()])


def build_command(take, dest, crop=None, audio_tracks=0, qp=16, hw=None):
    """crop: (x, y, w, h) in video pixels, or None for the whole picture.
    hw: use the graphics chip (VA-API); None asks gpu.ffmpeg_vaapi()."""
    hw = gpu.ffmpeg_vaapi() if hw is None else hw
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    if crop and hw:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
    cmd += ["-i", str(take)]

    filters = []
    if crop:
        x, y, w, h = crop
        scaler = f"scale_vaapi=w={w}:h={h}" if hw else "format=yuv420p"
        filters.append(f"[0:v]crop={w}:{h}:{x}:{y},{scaler},setsar=1[v]")
    if audio_tracks == 2:
        filters.append("[0:a:0]aresample=async=1:first_pts=0[a0];[0:a:1]aresample=async=1:first_pts=0[a1];"
                       "[a0][a1]amix=inputs=2:duration=longest:normalize=0[a]")

    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[v]" if crop else "0:v:0"]
    if audio_tracks == 2:
        cmd += ["-map", "[a]"]
    elif audio_tracks == 1:
        cmd += ["-map", "0:a:0"]

    if crop:
        cmd += video_encoder(hw, qp)
    else:
        cmd += ["-c:v", "copy"]
    if audio_tracks:
        cmd += ["-c:a", "aac", "-b:a", "256k"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(dest)]
    return cmd


def track_names(take_info, count):
    """Which audio track is which, in the order the recorder writes them."""
    names = []
    if take_info.get("system_audio", True):
        names.append("system")
    if take_info.get("microphone", True):
        names.append("mic")
    if len(names) != count:  # older or unusual takes: guess by position
        names = ["system", "mic"][:count] if count <= 2 else [f"a{i}" for i in range(count)]
    return names


def load_pointer(take_dir):
    """pointer.json written while recording: samples and clicks, or None."""
    try:
        return json.loads((Path(take_dir) / "pointer.json").read_text())
    except (OSError, ValueError):
        return None


def area_samples(pointer, crop_fraction):
    """Pointer samples in fractions of the recorded AREA (not the whole screen)."""
    if not pointer:
        return []
    cx, cy, cw, ch = crop_fraction or (0, 0, 1, 1)
    return [(s[0], (s[1] - cx) / cw, (s[2] - cy) / ch, *s[3:]) for s in pointer["samples"]]


def area_typing(pointer, crop_fraction):
    """Where text was typed [(t, x, y)], in fractions of the recorded area."""
    if not pointer:
        return []
    cx, cy, cw, ch = crop_fraction or (0, 0, 1, 1)
    return [(t, (x - cx) / cw, (y - cy) / ch) for t, x, y in pointer.get("typing", [])
            if cx <= x <= cx + cw and cy <= y <= cy + ch]


def write_zoom_commands(path, views, base, fps):
    """One ffmpeg command line per frame where the visible rectangle changes.

    views: per-frame (x, y, w, h) fractions of the base rectangle; base: the
    part of the take that is shown, in pixels (x, y, w, h).
    """
    bx, by, bw, bh = base
    last = None
    with open(path, "w") as f:
        for i, (vx, vy, vw, vh) in enumerate(views):
            w = max(16, int(round(vw * bw)) & ~1)
            h = max(16, int(round(vh * bh)) & ~1)
            x = min(bx + bw - w, bx + int(round(vx * bw))) & ~1
            y = min(by + bh - h, by + int(round(vy * bh))) & ~1
            rect = (w, h, x, y)
            if rect != last:
                # "crop@zoom": only the zoom's crop, not the ones blurs use.
                f.write(f"{i / fps:.4f} crop@zoom w {w}, crop@zoom h {h}, "
                        f"crop@zoom x {x}, crop@zoom y {y};\n")
                last = rect


def write_commands(plan, fps):
    """One sendcmd line per frame where something moves: the zoom's crop,
    the pointer and the click ring. Returns nothing; sets plan["has_commands"]."""
    bx, by, bw, bh = plan["base"]
    ax, ay, aw, ah = plan["area"]
    views = plan.get("zoom_views")
    p = plan.get("pointer")
    n = len(views) if views else 0
    last = {}
    count = 0
    with open(plan["commands"], "w") as f:
        for i in range(n):
            cmds = {}
            if views and i < len(views):
                vx, vy, vw, vh = views[i]
                w = max(16, int(round(vw * bw)) & ~1)
                h = max(16, int(round(vh * bh)) & ~1)
                cmds["crop@zoom"] = (w, h, min(bx + bw - w, bx + int(round(vx * bw))) & ~1,
                                     min(by + bh - h, by + int(round(vy * bh))) & ~1)
            parts = []
            for target, value in cmds.items():
                if last.get(target) == value:
                    continue
                last[target] = value
                if target == "crop@zoom":
                    w, h, x, y = value
                    parts += [f"crop@zoom w {w}", f"crop@zoom h {h}", f"crop@zoom x {x}",
                              f"crop@zoom y {y}"]
                else:
                    parts += [f"{target} x {value[0]}", f"{target} y {value[1]}"]
            if parts:
                f.write(f"{i / fps:.4f} " + ", ".join(parts) + ";\n")
                count += 1
    plan["has_commands"] = count > 0
    if p:
        # The pointer and its ring follow exact expressions of time (see
        # pointer.simplify): runtime commands would arrive a few frames early.
        from . import pointer as ptr
        times = [i / fps for i in range(len(p["frames"]))]
        xs = [ax + f[0] * aw - p["cursor_pad"] for f in p["frames"]]
        ys = [ay + f[1] * ah - p["cursor_pad"] for f in p["frames"]]
        keep = ptr.simplify(times, xs, ys)
        p["x_expr"] = ptr.linear_expr([(times[k], xs[k]) for k in keep])
        p["y_expr"] = ptr.linear_expr([(times[k], ys[k]) for k in keep])
        steps_x, steps_y, last_pos = [], [], "none"
        half = p["ring_box"] // 2
        for i, r in enumerate(p["rings"]):
            pos = None if r is None else (int(round(ax + r[1] * aw)) - half,
                                          int(round(ay + r[2] * ah)) - half)
            if pos != last_pos:
                steps_x.append((times[i], -999 if pos is None else pos[0]))
                steps_y.append((times[i], -999 if pos is None else pos[1]))
                last_pos = pos
        p["ring_x_expr"] = ptr.step_expr(steps_x)
        p["ring_y_expr"] = ptr.step_expr(steps_y)


def plan_picture(take_dir, doc, crop_fraction, size, duration, fps, work_dir):
    """Everything the export needs to draw the finished picture.

    Shares compose.layout and the rendered images with the editor preview,
    so the saved video matches what was shown.
    """
    from . import compose, motion

    vw, vh = size
    area = crop_pixels(crop_fraction, size) if crop_fraction else (0, 0, vw, vh)
    lay = compose.layout(area[2], area[3], doc["style"])
    sx, sy, sw, sh = lay["source"]
    base = (area[0] + (int(round(sx * area[2])) & ~1), area[1] + (int(round(sy * area[3])) & ~1),
            compose.even(sw * area[2]), compose.even(sh * area[3]))
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    plan = {"layout": lay, "area": area, "base": base, "zoom_views": None}
    if em_has_zoom(doc):
        pointer = load_pointer(take_dir)
        samples = [(t, (x - sx) / sw, (y - sy) / sh, *rest)
                   for t, x, y, *rest in area_samples(pointer, crop_fraction)]
        typing = [(t, (x - sx) / sw, (y - sy) / sh)
                  for t, x, y in area_typing(pointer, crop_fraction)]
        views = motion.viewports(segments_in_base(doc["zoom"]["segments"], lay["source"]),
                                 doc["zoom"]["amount"], samples, duration, fps, typing=typing)
        plan["zoom_views"] = views
    plan["assets"] = render_assets(doc, lay, work_dir, take_dir)
    plan["pointer"] = None
    ptr_doc = doc.get("pointer") or {}
    pointer_data = load_pointer(take_dir) if ptr_doc.get("drawn") and ptr_doc.get("show") else None
    if pointer_data:
        from . import pointer as ptr
        frames, rings = ptr.track(area_samples(pointer_data, crop_fraction), duration, fps,
                                  ptr_doc["smoothing"])
        px = ptr.pixel_size(area[3], ptr_doc["size"])
        layers = render_pointer_layers(frames, rings, px, fps, work_dir, ptr_doc["rings"])
        plan["pointer"] = {"frames": frames, "rings": rings, **layers}
    plan["commands"] = str(Path(work_dir) / "commands.txt")
    write_commands(plan, fps)
    # Blurs cover parts of the recorded area: convert them to take pixels.
    plan["blurs"] = []
    for b in doc.get("blurs", []):
        x, y, w, h = b["rect"]
        px = (area[0] + int(x * area[2]), area[1] + int(y * area[3]),
              max(8, int(w * area[2])), max(8, int(h * area[3])))
        px = (px[0] & ~1, px[1] & ~1, px[2] & ~1, px[3] & ~1)
        plan["blurs"].append({**b, "px": px,
                              "sigma": compose.blur_sigma(px[2], px[3], b["strength"])})
    return plan


def segments_in_base(segments, source):
    """Zoom spots are stored in fractions of the recorded area; the zoom
    works inside the shown part (a shape can crop it), so convert them."""
    sx, sy, sw, sh = source
    out = []
    for seg in segments:
        seg = dict(seg)
        if seg.get("focus"):
            fx, fy = seg["focus"]
            seg["focus"] = [min(1, max(0, (fx - sx) / sw)), min(1, max(0, (fy - sy) / sh))]
        out.append(seg)
    return out


def render_pointer_layers(frames, rings, pointer_px, fps, out_dir, with_rings):
    """Two small transparent videos: the pointer (fading, pressing in) and
    the click ring. Moved over the recording frame by frame by ffmpeg.

    Returns {"cursor": path, "ring": path or None, "cursor_pad": px,
    "ring_box": px}. Identical frames are drawn once and reused.
    """
    import cairo

    from . import pointer as ptr

    out = Path(out_dir)
    pad = max(2, int(pointer_px * 0.12))
    cw = int(pointer_px * ptr.ARROW_BOX[0] / ptr.ARROW_BOX[1]) + 2 * pad + 2
    ch = int(pointer_px) + 2 * pad + 2
    cw, ch = cw + cw % 2, ch + ch % 2
    r_max = ptr.ring_shape(1.0, pointer_px)[0] + 3 * pointer_px / 34
    rb = int(2 * r_max + 4)
    rb += rb % 2

    def encode(path, w, h, frame_bytes):
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "qtrle", str(path)],
            stdin=subprocess.PIPE)
        for data in frame_bytes:
            proc.stdin.write(data)
        proc.stdin.close()
        proc.wait()

    cache = {}

    def cursor_frame(scale, alpha):
        key = (round(scale, 2), round(alpha, 2))
        if key not in cache:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, cw, ch)
            if key[1] > 0.005:
                cr = cairo.Context(surface)
                cr.translate(pad, pad)
                ptr.draw_arrow(cr, pointer_px * key[0], key[1])
            surface.flush()
            cache[key] = bytes(surface.get_data())
        return cache[key]

    cursor_path = out / "cursor.mov"
    encode(cursor_path, cw, ch, (cursor_frame(sc, op) for _x, _y, op, sc in frames))

    ring_path = None
    if with_rings:
        rcache = {}

        def ring_frame(r):
            key = None if r is None else round(r[0], 2)
            if key not in rcache:
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, rb, rb)
                if key is not None:
                    radius, width, alpha = ptr.ring_shape(key, pointer_px)
                    cr = cairo.Context(surface)
                    r_in = max(0.5, radius - width / 2)
                    # A faint dark edge keeps the white ring visible on white pages.
                    cr.set_source_rgba(0, 0, 0, alpha * 0.6)
                    cr.set_line_width(width + 2 * max(1.0, width * 0.5))
                    cr.arc(rb / 2, rb / 2, r_in, 0, 6.2832)
                    cr.stroke()
                    cr.set_source_rgba(1, 1, 1, alpha * 1.6)
                    cr.set_line_width(width)
                    cr.arc(rb / 2, rb / 2, r_in, 0, 6.2832)
                    cr.stroke()
                surface.flush()
                rcache[key] = bytes(surface.get_data())
            return rcache[key]

        ring_path = out / "ring.mov"
        encode(ring_path, rb, rb, (ring_frame(r) for r in rings))
    return {"cursor": cursor_path, "ring": ring_path, "cursor_pad": pad, "ring_box": rb}


def em_has_zoom(doc):
    from . import edit_model as em
    return em.has_zoom(doc)


def render_assets(doc, lay, out_dir, take_dir):
    """Background, corner mask, captions and pictures as PNGs, with places.

    Files are named after their content, so unchanged ones are reused.
    """
    import hashlib

    from . import compose

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    W, H = lay["canvas"]
    x, y, rw, rh = lay["rect"]

    def name(*parts):
        return out / (hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16] + ".png")

    assets = {"background": None, "mask": None, "texts": [], "images": []}
    bg = doc["style"]["background"]
    if lay["background"]:
        path = name("bg", W, H, bg)
        if not path.exists():
            compose.render_background(path, W, H, bg)
        assets["background"] = path
        if lay["radius"] >= 1:
            mpath = name("mask", rw, rh, round(lay["radius"], 1))
            if not mpath.exists():
                compose.render_mask(mpath, rw, rh, lay["radius"])
            assets["mask"] = mpath
    for t in doc.get("texts", []):
        height = t["size"] * H
        path = name("text", t["text"], round(height), t["color"])
        meta = path.with_suffix(".json")
        if not (path.exists() and meta.exists()):
            size = compose.render_text(path, t["text"], height, compose.TEXT_COLORS[t["color"]])
            meta.write_text(json.dumps(size))
        w, h = json.loads(meta.read_text())
        assets["texts"].append((t, path, (*compose.anchor_point(t["anchor"], (W, H), (w, h)), w, h)))
    for im in doc.get("images", []):
        src = Path(take_dir) / im["file"]
        if not src.exists():
            continue
        path = name("image", im["file"], round(im["size"] * W), round(H * 0.6))
        meta = path.with_suffix(".json")
        if not (path.exists() and meta.exists()):
            size = compose.render_image(path, src, im["size"] * W, H * 0.6)
            meta.write_text(json.dumps(size))
        w, h = json.loads(meta.read_text())
        assets["images"].append((im, path, (*compose.anchor_point(im["anchor"], (W, H), (w, h)), w, h)))
    return assets


def _picture_chain(plan, fps, duration, first_input):
    """The video filter graph for zoom, blur, shape, background and overlays.

    Runs on the CPU between GPU decoding and encoding. first_input is the
    index the extra image inputs start at. Returns (chain, extra input args).
    """
    from . import compose

    lay = plan["layout"]
    W, H = lay["canvas"]
    rx, ry, rw, rh = lay["rect"]
    bx, by, bw, bh = plan["base"]
    inputs, idx = [], first_input
    loop = lambda path: ["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}", "-i", str(path)]  # noqa: E731

    parts = ["[0:v]hwdownload,format=nv12[c0]"]
    cur = "c0"
    for k, b in enumerate(plan["blurs"]):
        x, y, w, h = b["px"]
        parts.append(f"[{cur}]split[ba{k}][bb{k}];[bb{k}]crop={w}:{h}:{x}:{y},gblur=sigma={b['sigma']:.1f}[bl{k}];"
                     f"[ba{k}][bl{k}]overlay={x}:{y}:enable='between(t,{b['start']:.3f},{b['end']:.3f})'[cb{k}]")
        cur = f"cb{k}"
    p = plan.get("pointer")
    if p:
        # The pointer (and ring) go on the recording itself, before the zoom,
        # so a zoom enlarges them together with everything else.
        inputs += ["-i", str(p["cursor"])]
        parts.append(f"[{idx}:v]format=rgba[cur_s];[{cur}][cur_s]overlay@cursor="
                     f"x='{p['x_expr']}':y='{p['y_expr']}':eof_action=pass[cp]")
        idx += 1
        cur = "cp"
        if p["ring"]:
            inputs += ["-i", str(p["ring"])]
            parts.append(f"[{idx}:v]format=rgba[ring_s];[{cur}][ring_s]overlay@ring="
                         f"x='{p['ring_x_expr']}':y='{p['ring_y_expr']}':eof_action=pass[rp]")
            idx += 1
            cur = "rp"
    # The zoom's crop size changes over time, which only commands can do;
    # sendcmd sits right before it so they arrive as close to their frame as possible.
    send = f"sendcmd=f='{plan['commands']}'," if plan.get("has_commands") else ""
    parts.append(f"[{cur}]{send}crop@zoom={bw}:{bh}:{bx}:{by},scale={rw}:{rh}:flags=lanczos,setsar=1[vs]")
    cur = "vs"

    assets = plan["assets"]
    if assets["background"]:
        inputs += loop(assets["background"])
        bg = idx
        idx += 1
        if assets["mask"]:
            inputs += loop(assets["mask"])
            parts.append(f"[{cur}]format=yuva420p[va];[{idx}:v]format=gray[mk];[va][mk]alphamerge[vr]")
            idx += 1
            cur = "vr"
        parts.append(f"[{bg}:v]format=yuv420p[bgv];[bgv][{cur}]overlay={rx}:{ry}:shortest=1[vc]")
        cur = "vc"

    for kind, items, fade in (("t", assets["texts"], compose.TEXT_FADE),
                              ("i", assets["images"], compose.IMAGE_FADE)):
        for k, (item, path, (x, y, w, h)) in enumerate(items):
            inputs += loop(path)
            a, b = item["start"], item["end"]
            ramp = min(fade, (b - a) / 2)
            mix = f",colorchannelmixer=aa={item['opacity']:.2f}" if kind == "i" else ""
            parts.append(f"[{idx}:v]format=rgba{mix},fade=t=in:st={a:.3f}:d={ramp:.3f}:alpha=1,"
                         f"fade=t=out:st={b - ramp:.3f}:d={ramp:.3f}:alpha=1[{kind}{k}];"
                         f"[{cur}][{kind}{k}]overlay={int(x)}:{int(y)}:"
                         f"enable='between(t,{a:.3f},{b:.3f})':eof_action=pass[o{kind}{k}]")
            idx += 1
            cur = f"o{kind}{k}"
    return ";".join(parts), cur, inputs


def atempo_chain(speed):
    """Changes sound speed without changing pitch; atempo takes 0.5-2 per step."""
    steps = []
    while speed > 2.0 + 1e-9:
        steps.append(2.0)
        speed /= 2.0
    while speed < 0.5 - 1e-9:
        steps.append(0.5)
        speed /= 0.5
    if abs(speed - 1.0) > 1e-6:
        steps.append(speed)
    return "".join(f",atempo={v:.6g}" for v in steps)


def build_edit_command(take, dest, doc, names, crop=None, fps=60, qp=16, mic_clean=None,
                       plan=None, duration=None, out=None, hw=None):
    """Applies trim, cuts and per-track volume, plus the picture plan if any.

    Video: frames inside the kept parts are selected (frame-accurate) and
    re-timed. Without a picture plan everything stays on the GPU; with one
    (zoom, blur, background, overlays), frames are composed on the CPU
    between GPU decoding and encoding. Sound: each kept part is cut out
    sample-accurately and joined, so it stays in step across every cut.
    """
    from . import edit_model as em

    hw = gpu.ffmpeg_vaapi() if hw is None else hw
    # out: how the file is made (see output_settings). None: full quality.
    out = out or {}
    speed = out.get("speed", 1.0)
    out_fps = out.get("fps", fps)
    size = out.get("size")
    scale = f",{_scaler(hw, size[0], size[1])}" if size else ""
    rate = fps * speed
    # Kept parts with their rate: a sped-up part (idle moment) keeps every
    # Nth frame, so after re-timing it plays N times faster. Zoom, pointer
    # and overlays are applied before this, in recording time, so they stay
    # in step.
    kept = em.pieces(doc)
    n = len(kept)
    # [start, end): including the end frame would add one frame per part.
    sel = "+".join(f"gte(t,{a:.4f})*lt(t,{b:.4f})" + (f"*not(mod(n,{r}))" if r > 1 else "")
                   for a, b, r in kept)
    extra = []
    if plan:
        first = 2 if mic_clean else 1
        picture, last, extra = _picture_chain(plan, fps, duration, first)
        upload = "format=nv12,hwupload" if hw else "format=yuv420p"
        chains = [picture + f";[{last}]{upload},select='{sel}',"
                            f"setpts=N/({rate:g}*TB){scale},setsar=1[v]"]
    else:
        if crop and not size:
            scale = f",{_scaler(hw, crop[2], crop[3])}"
        chains = [f"[0:v]select='{sel}',setpts=N/({rate:g}*TB)"
                  + (f",crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}" if crop else "")
                  + (f"{scale},setsar=1" if scale else "")
                  + "[v]"]

    live = []
    tempo = atempo_chain(speed)
    for i, name in enumerate(names if out.get("audio", True) else ()):
        if doc["muted"].get(name) or doc["gain"].get(name, 1.0) <= 0.001:
            continue
        gain = doc["gain"].get(name, 1.0)
        parts = "".join(f"[s{i}_{k}]" for k in range(n))
        # The microphone can come from its noise-cleaned copy (second input).
        src = "[1:a:0]" if (name == "mic" and mic_clean) else f"[0:a:{i}]"
        # Every track on one timeline from zero: a track that starts late (the
        # system sound, delayed for the speakers) keeps its place, as silence.
        pre = "aresample=async=1:first_pts=0" + _sync_shift(doc, name)
        chain = f"{src}{pre},asplit={n}{parts};" if n > 1 else f"{src}{pre}[s{i}_0];"
        for k, (a, b, r) in enumerate(kept):
            # A sped-up part's sound is shortened to match (it's quiet: an idle moment).
            chain += (f"[s{i}_{k}]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS"
                      f"{atempo_chain(r)}[t{i}_{k}];")
        joined = "".join(f"[t{i}_{k}]" for k in range(n))
        chain += f"{joined}concat=n={n}:v=0:a=1{tempo},volume={gain:.3f}[a{i}]"
        chains.append(chain)
        live.append(f"[a{i}]")
    if len(live) > 1:
        chains.append(f"{''.join(live)}amix=inputs={len(live)}:duration=longest:normalize=0[a]")
    elif len(live) == 1:
        chains[-1] = chains[-1][: -len(live[0])] + "[a]"

    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-nostats", "-progress", "pipe:1"]
    if hw:
        cmd += ["-init_hw_device", "vaapi=va", "-filter_hw_device", "va",
                "-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi"]
    cmd += ["-i", str(take)] + (["-i", str(mic_clean)] if mic_clean else []) + extra
    graph = ";".join(chains)
    if plan and len(graph) > 20000:
        # Long graphs (a pointer path) go in a file: command lines have limits.
        graph_file = Path(plan["commands"]).with_name("graph.txt")
        graph_file.write_text(graph)
        cmd += ["-/filter_complex", str(graph_file), "-map", "[v]"]
    else:
        cmd += ["-filter_complex", graph, "-map", "[v]"]
    if live:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", out.get("audio_bitrate", "256k")]
    if out.get("bitrate"):
        # Aiming at a file size: a bit rate instead of a fixed quality.
        b = int(out["bitrate"])
        cmd += video_encoder(hw, bitrate=b)
    else:
        cmd += video_encoder(hw, out.get("qp", qp))
    cmd += ["-r", f"{out_fps:g}", "-movflags", "+faststart", "-f", "mp4", str(dest)]
    return cmd


def _sync_shift(doc, name):
    """Moves the system sound later (+) or earlier (-) by the editor's sync."""
    if name != "system":
        return ""
    d = float((doc.get("sync") or {}).get("system", 0.0))
    if d > 0.0005:
        return f",adelay=delays={d * 1000:.0f}:all=1"
    if d < -0.0005:
        return f",atrim=start={-d:.4f},asetpts=PTS-STARTPTS"
    return ""


def _scaler(hw, w, h):
    return f"scale_vaapi=w={w}:h={h}" if hw else f"scale={w}:{h}:flags=lanczos"


def video_encoder(hw, qp=16, bitrate=None):
    """ffmpeg options for H.264: on the graphics chip, or x264 in software."""
    if hw:
        if bitrate:
            return ["-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", str(bitrate),
                    "-maxrate", str(int(bitrate * 1.25)), "-bufsize", str(bitrate * 2)]
        return ["-c:v", "h264_vaapi", "-qp", str(qp)]
    sw = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if bitrate:
        return sw + ["-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.25)), "-bufsize", str(bitrate * 2)]
    # x264's CRF looks a little sharper than VA-API's QP at the same number.
    return sw + ["-crf", str(qp + 2)]


# Quality presets: Small is half the size; Balanced is the
# full picture with a little less detail per pixel.
QUALITY_QP = {"high": 16, "balanced": 21, "small": 23}
FIT_AUDIO_BPS = 128_000
FIT_MAX_EDGE = 1920
FIT_MIN_VIDEO_BPS = 150_000


def even_size(w, h):
    return max(2, int(round(w)) & ~1), max(2, int(round(h)) & ~1)


def picture_size(size, crop_fraction, doc):
    """Size of the finished picture at full quality (with background or shape)."""
    from . import compose
    area = crop_pixels(crop_fraction, size) if crop_fraction else (0, 0, *size)
    return tuple(compose.layout(area[2], area[3], doc["style"])["canvas"])


def fit_plan(duration, size, fps, has_audio, target_mb, bitrate_scale=1.0):
    """Bit rate, size and frame rate that fit a file of target_mb, or None.

    Sharing plan: at most 30 fps and 1920 px, a bit rate
    from the size budget, and a smaller picture when that rate is too low
    for the full one to stay sharp.
    """
    if duration <= 0:
        return None
    out_fps = min(30, fps)
    w, h = size
    edge = max(w, h)
    if edge > FIT_MAX_EDGE:
        w, h = w * FIT_MAX_EDGE / edge, h * FIT_MAX_EDGE / edge
    audio = FIT_AUDIO_BPS if has_audio else 0
    available = int(target_mb * 1_000_000 * 8 * 0.94 / duration) - audio
    ceiling = int(w * h * out_fps * 0.085)
    video = int(min(available, max(800_000, ceiling)) * min(1.0, bitrate_scale))
    if video < FIT_MIN_VIDEO_BPS:
        return None
    sharp_pixels = video / (out_fps * 0.055)
    k = min(1.0, (sharp_pixels / max(1.0, w * h)) ** 0.5)
    k = max(k, min(1.0, 640 / max(w, h)))
    return {"size": even_size(w * k, h * k), "fps": out_fps, "bitrate": video,
            "audio_bitrate": f"{audio // 1000}k" if audio else "128k"}


def output_settings(doc, size, crop_fraction, fps, duration_out, has_audio, bitrate_scale=1.0):
    """The 'out' argument of build_edit_command for the document's export choices.

    Returns None when the recording is too long to fit the chosen size.
    """
    from . import edit_model as em
    ex = em.normalize_export(doc.get("export"))
    out = {"speed": ex["speed"], "fps": fps}
    full = picture_size(size, crop_fraction, doc)
    if ex["fit_mb"]:
        plan = fit_plan(duration_out, full, fps, has_audio, ex["fit_mb"], bitrate_scale)
        if plan is None:
            return None
        out.update(plan)
        if out["size"] == full:
            out.pop("size")
        return out
    out["qp"] = QUALITY_QP[ex["quality"]]
    if ex["quality"] == "small":
        out["size"] = even_size(full[0] / 2, full[1] / 2)
    return out


def gif_size(full, gif_size_name):
    from . import edit_model as em
    edge = em.GIF_SIZES.get(gif_size_name, 600)
    k = min(1.0, edge / max(full))
    return even_size(full[0] * k, full[1] * k)


def build_gif_command(src, dest, size, fps):
    """One shared palette for the clip; screen pictures keep crisp edges."""
    w, h = size
    graph = (f"[0:v]fps={fps},scale={w}:{h}:flags=lanczos,split[a][b];"
             f"[a]palettegen=max_colors=256:stats_mode=full[p];"
             f"[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    return ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-nostats", "-progress", "pipe:1",
            "-i", str(src), "-filter_complex", graph, "-loop", "0", "-f", "gif", str(dest)]


class SaveJob:
    """One running export, maybe in several steps. cancel() stops it and leaves no file."""

    def __init__(self, tmp):
        self.proc = None
        self.tmp = tmp
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self.proc is not None:
            self.proc.force_exit()


def _run_step(job, cmd, total, on_progress, then):
    """Runs one ffmpeg command; then(ok, error_text). total: seconds, for progress."""
    flags = Gio.SubprocessFlags.STDERR_PIPE | (Gio.SubprocessFlags.STDOUT_PIPE if total
                                               else Gio.SubprocessFlags.NONE)
    try:
        proc = Gio.Subprocess.new(cmd, flags)
    except GLib.Error as e:
        GLib.idle_add(then, False, str(e))
        return
    job.proc = proc

    if total:  # always drain progress output, or ffmpeg could block on a full pipe
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())

        def read_line(src, result):
            try:
                line, _len = src.read_line_finish_utf8(result)
            except GLib.Error:
                return
            if line is None:
                return
            if line.startswith("out_time_us="):
                try:
                    if on_progress:
                        on_progress(max(0.0, min(1.0, int(line.split("=", 1)[1]) / 1e6 / total)))
                except ValueError:
                    pass
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, read_line)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, read_line)

    def finished(p, result):
        try:
            p.wait_finish(result)
        except GLib.Error:
            pass
        err = ""
        try:
            pipe = p.get_stderr_pipe()
            data = pipe.read_bytes(65536, None).get_data() if pipe else b""
            err = data.decode(errors="replace")
        except GLib.Error:
            pass
        then(p.get_successful() and not job.cancelled, (err or "ffmpeg failed").strip()[-500:])

    proc.wait_async(None, finished)


TOO_LONG = "too long"   # message when a recording can't fit the chosen size


def save_async(take, dest, crop_fraction, on_done, doc=None, take_info=None,
               on_progress=None, duration=None, gif=False):
    """Runs ffmpeg without blocking the app. on_done(ok, message).

    crop_fraction: (x, y, w, h) as 0-1 fractions of the picture, or None.
    doc: the editor's document; when it changes nothing, the fast no-loss
    path is used. on_progress(fraction 0-1) is called while saving edits.
    gif: make an animated GIF (the edited video first, then the GIF from it).
    A file-size limit is met by saving again with less if the first try
    comes out too big.
    """
    from . import edit_model as em

    dest = Path(dest)
    tmp = dest.with_name(f".{dest.stem}.saving{dest.suffix}")
    work = tmp.with_name(tmp.stem + ".parts")
    job = SaveJob(tmp)
    progress = on_progress or (lambda f: None)

    def finish(ok, message):
        shutil.rmtree(work, ignore_errors=True)
        if ok and not job.cancelled and tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, dest)
            on_done(True, str(dest))
            return
        tmp.unlink(missing_ok=True)
        on_done(False, "cancelled" if job.cancelled else message)

    size = video_size(take)
    crop = crop_pixels(crop_fraction, size) if crop_fraction else None
    count = audio_track_count(take)
    edited = doc is not None and duration and (gif or not em.is_unedited(doc, duration))
    if not edited:
        _run_step(job, build_command(take, tmp, crop, count), None, None, finish)
        return job

    info = take_info or {}
    fps = info.get("fps", 60)
    names = track_names(info, count)
    plan = None
    if em.has_zoom(doc) or em.needs_compose(doc):
        plan = plan_picture(Path(take).parent, doc, crop_fraction, size, duration, fps, work)
    mic_clean = None
    if (doc.get("denoise") or {}).get("mic"):
        from . import denoise
        path = denoise.clean_path(Path(take).parent, doc["denoise"]["strength"])
        mic_clean = path if path.exists() else None
    total = em.output_duration(doc)
    ex = em.normalize_export(doc.get("export"))
    has_audio = any(not doc["muted"].get(n) and doc["gain"].get(n, 1.0) > 0.001 for n in names)

    def command(target, out):
        return build_edit_command(take, target, doc, names, crop, fps=fps, mic_clean=mic_clean,
                                  plan=plan, duration=duration, out=out)

    if gif:
        work.mkdir(parents=True, exist_ok=True)
        video = work / "gif-source.mp4"
        # The edited video at the GIF's frame rate, full size and near
        # lossless; the GIF is then scaled from it with a good filter.
        out = {"speed": ex["speed"], "fps": ex["gif_fps"], "qp": 12, "audio": False}

        def made_video(ok, message):
            if not ok:
                finish(False, message)
                return
            full = picture_size(size, crop_fraction, doc)
            cmd = build_gif_command(video, tmp, gif_size(full, ex["gif_size"]), ex["gif_fps"])
            _run_step(job, cmd, total, lambda f: progress(0.6 + 0.4 * f), finish)

        _run_step(job, command(video, out), total, lambda f: progress(0.6 * f), made_video)
        return job

    limit = ex["fit_mb"] * 1_000_000
    attempt = {"n": 0, "scale": 1.0}

    def run():
        out = output_settings(doc, size, crop_fraction, fps, total, has_audio, attempt["scale"])
        if out is None:
            GLib.idle_add(finish, False, TOO_LONG)
            return
        # A retry restarts the bar: it reads more honestly than a jump back.
        _run_step(job, command(tmp, out), total, progress, checked)

    def checked(ok, message):
        if ok and limit and tmp.exists() and tmp.stat().st_size > limit and attempt["n"] < 3:
            # Too big: try again with a lower rate.
            actual = tmp.stat().st_size
            attempt["n"] += 1
            attempt["scale"] = min(attempt["scale"] * 0.9,
                                   attempt["scale"] * limit * 0.94 / actual)
            run()
            return
        if ok and limit and tmp.exists() and tmp.stat().st_size > limit:
            finish(False, TOO_LONG)
            return
        finish(ok, message)

    run()
    return job
