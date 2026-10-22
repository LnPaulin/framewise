"""What this computer's graphics can do, so Framewise runs on any machine.

Intel and AMD graphics use VA-API: recording, preview, camera filters and
export run on the graphics chip. Elsewhere (NVIDIA with its own driver, a
virtual machine, no driver), the same work is done in software: x264 for
H.264, videoconvert/videoscale for conversions. Slower, but it works.

Checked once per run; FRAMEWISE_NO_VAAPI=1 forces the software path (for
testing it on a machine that has VA-API).
"""

import functools
import os
import subprocess

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)


def _has(*elements):
    return all(Gst.ElementFactory.find(e) is not None for e in elements)


@functools.cache
def gst_va():
    """GStreamer's VA plugin found a usable graphics device (it only
    registers its elements when one works)."""
    if os.environ.get("FRAMEWISE_NO_VAAPI"):
        return False
    return _has("vapostproc", "vah264enc", "vah264dec")


def convert():
    """Colour conversion and scaling (as one GStreamer step)."""
    return "vapostproc" if gst_va() else "videoconvert ! videoscale"


def h264_decoder():
    if gst_va():
        return "vah264dec"
    for name in ("avdec_h264", "openh264dec"):
        if _has(name):
            return name
    return "decodebin"


def jpeg_decoder():
    return "vajpegdec" if gst_va() and _has("vajpegdec") else "jpegdec"


def h264_encoder(qp, keyframes):
    """An encoder for recording at a fixed quality (qp: lower is better)."""
    if gst_va():
        return (f"vah264enc rate-control=cqp qpi={qp[0]} qpp={qp[1]} qpb={qp[1]} "
                f"key-int-max={keyframes} b-frames=0 target-usage=2")
    if _has("x264enc"):
        # Fast enough for live recording on a laptop CPU, still sharp text.
        return (f"x264enc pass=quant quantizer={qp[0]} speed-preset=superfast "
                f"key-int-max={keyframes} bframes=0 threads=0")
    return f"openh264enc complexity=low gop-size={keyframes}"


@functools.cache
def ffmpeg_vaapi():
    """ffmpeg can encode H.264 on the graphics chip (tried once, quickly)."""
    if os.environ.get("FRAMEWISE_NO_VAAPI"):
        return False
    try:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-init_hw_device", "vaapi=va",
             "-filter_hw_device", "va", "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.2",
             "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-"],
            capture_output=True, timeout=15)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def summary():
    """One line for Settings → About."""
    if gst_va() and ffmpeg_vaapi():
        return "Hardware (VA-API) for recording and export"
    if gst_va():
        return "Hardware for recording; software for export"
    return "Software (VA-API not available): higher CPU usage"
