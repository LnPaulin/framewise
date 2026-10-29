"""Make a video fit: a copy of any video, small enough to send.

Chats and email limit attachments (Discord 10 MB, email 25 MB...). This
re-encodes a video to land just under a chosen size: the bit rate comes from
the size budget, and the picture gets smaller when that rate is too low to
keep it sharp (export.fit_plan, the same plan the editor's size limit uses).
If the first try comes out too big, it tries again with a lower rate.
The copy is saved next to the original as "Name (10 MB).mp4".
"""

import json
import subprocess
from pathlib import Path

from . import export, gpu

PRESETS = [   # (MB, label)
    (10, "Discord"),
    (25, "Email"),
    (50, "Easy to share"),
]
MAX_TRIES = 3
UNDER = 0.7          # a copy smaller than this share of the size is tried once more, sharper
MAX_BOOST = 3.0


class Info:
    """What ffprobe says about a video."""

    def __init__(self, path):
        self.path = Path(path)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and not (s.get("disposition") or {}).get("attached_pic")), None)
        if video is None:
            raise ValueError("This file has no video")
        self.width, self.height = int(video["width"]), int(video["height"])
        # Phone videos are often stored sideways with a note to turn them.
        rotation = 0
        for side in video.get("side_data_list") or []:
            if "rotation" in side:
                rotation = int(float(side["rotation"]))
        rotation = rotation or int((video.get("tags") or {}).get("rotate", 0) or 0)
        if abs(rotation) % 180 == 90:
            self.width, self.height = self.height, self.width
        self.fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")) or 30.0
        self.has_audio = any(s.get("codec_type") == "audio" for s in streams)
        fmt = data.get("format") or {}
        self.duration = float(fmt.get("duration") or video.get("duration") or 0)
        self.bytes = int(fmt.get("size") or self.path.stat().st_size)
        if self.duration <= 0:
            raise ValueError("The video's length couldn't be read")

    @property
    def megabytes(self):
        return self.bytes / 1_000_000


def _rate(text):
    try:
        a, b = str(text).split("/")
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def destination(src, target_mb):
    """'Name (10 MB).mp4' next to the original (or in Videos when that folder
    can't be written), never replacing a file."""
    src = Path(src)
    folder = src.parent
    if not _writable(folder):
        folder = Path.home() / "Videos"
        folder.mkdir(parents=True, exist_ok=True)
    stem = f"{src.stem} ({target_mb:g} MB)"
    path = folder / f"{stem}.mp4"
    n = 2
    while path.exists():
        path = folder / f"{stem} {n}.mp4"
        n += 1
    return path


def _writable(folder):
    import os
    return folder.is_dir() and os.access(folder, os.W_OK)


def build_command(info, dest, plan, hw=None):
    """ffmpeg command for one try. Decoding is in software (a video from
    anywhere may use any format); encoding on the graphics chip when it can."""
    hw = gpu.ffmpeg_vaapi() if hw is None else hw
    w, h = plan["size"]
    vf = []
    if info.fps > plan["fps"] + 0.5:
        vf.append(f"fps={plan['fps']}")
    vf.append(f"scale={w}:{h}:flags=lanczos,setsar=1")
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-nostats", "-progress", "pipe:1"]
    if hw:
        cmd += ["-init_hw_device", "vaapi=va", "-filter_hw_device", "va"]
        vf.append("format=nv12,hwupload")
    cmd += ["-i", str(info.path), "-map", "0:v:0", "-vf", ",".join(vf)]
    cmd += export.video_encoder(hw, bitrate=plan["bitrate"])
    if info.has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", plan["audio_bitrate"], "-ac", "2"]
    cmd += ["-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", str(dest)]
    return cmd


def fit_async(info, target_mb, on_progress, on_done, hw=None):
    """Makes the copy. on_progress(0-1); on_done(ok, path or message).
    Returns a job whose cancel() stops it and removes the half-made file."""
    dest = destination(info.path, target_mb)
    tmp = dest.with_name(f".{dest.stem}.making.mp4")
    kept = dest.with_name(f".{dest.stem}.fits.mp4")      # a copy that fits, while a sharper one is tried
    job = export.SaveJob(tmp)
    limit = target_mb * 1_000_000
    state = {"try": 0, "scale": 1.0, "boosted": False}

    def finish(ok, message):
        if ok and not job.cancelled and tmp.exists() and 0 < tmp.stat().st_size <= limit:
            tmp.replace(dest)
            kept.unlink(missing_ok=True)
            on_done(True, str(dest))
            return
        tmp.unlink(missing_ok=True)
        if not job.cancelled and kept.exists():
            kept.replace(dest)          # the sharper try didn't fit: the first one does
            on_done(True, str(dest))
            return
        kept.unlink(missing_ok=True)
        on_done(False, "cancelled" if job.cancelled else message)

    def run():
        plan = export.fit_plan(info.duration, (info.width, info.height), info.fps, info.has_audio,
                               target_mb, state["scale"])
        if plan is None:
            finish(False, too_long_message(info, target_mb))
            return
        # A retry restarts the bar: it reads more honestly than a jump back.
        export._run_step(job, build_command(info, tmp, plan, hw), info.duration, on_progress, checked)

    def checked(ok, message):
        size = tmp.stat().st_size if ok and not job.cancelled and tmp.exists() else 0
        if size and size < limit * UNDER and not state["boosted"] and state["try"] < MAX_TRIES - 1:
            # Well under: keep this one and try once more with a higher rate.
            state["boosted"] = True
            state["try"] += 1
            tmp.replace(kept)
            state["scale"] = min(MAX_BOOST, state["scale"] * limit * 0.92 / size)
            run()
            return
        if state["boosted"] and size > limit:
            finish(False, "")                 # falls back to the copy that fit
            return
        if ok and not job.cancelled and tmp.exists() and tmp.stat().st_size > limit:
            state["try"] += 1
            if state["try"] >= MAX_TRIES:
                finish(False, too_long_message(info, target_mb))
                return
            actual = tmp.stat().st_size
            state["scale"] = min(state["scale"] * 0.9, state["scale"] * limit * 0.94 / actual)
            run()
            return
        finish(ok, message)

    run()
    return job


def too_long_message(info, target_mb):
    minutes = info.duration / 60
    return (f"This video is too long to fit {target_mb:g} MB and still be watchable "
            f"({minutes:.0f} min). Choose a larger size, or trim it in the editor first.")
