"""Removes background noise (keyboard, fan, hum) from the microphone track.

Uses RNNoise, a small neural network built into ffmpeg (the arnndn filter),
with the model files shipped in framewise/models. The cleaned track is
made once per strength and kept next to the take, so the preview plays
exactly what will be saved. The original recording is never changed.
"""

from pathlib import Path

from gi.repository import Gio, GLib

MODELS_DIR = Path(__file__).with_name("models")
MODELS = {"normal": MODELS_DIR / "voice.rnnn", "strong": MODELS_DIR / "voice-strong.rnnn"}


def clean_path(take_dir, strength):
    return Path(take_dir) / f"mic-clean-{strength}.flac"


def make_clean_async(take, track_index, take_dir, strength, on_done):
    """on_done(path or None) once the cleaned microphone track is ready."""
    out = clean_path(take_dir, strength)
    if out.exists() and out.stat().st_size > 0:
        GLib.idle_add(on_done, out)
        return
    model = MODELS.get(strength, MODELS["normal"])
    tmp = out.with_name(f".{out.stem}.{GLib.get_monotonic_time()}.part.flac")
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(take),
           "-map", f"0:a:{track_index}", "-af", f"arnndn=m='{model}'",
           "-c:a", "flac", str(tmp)]
    try:
        proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDERR_SILENCE)
    except GLib.Error:
        GLib.idle_add(on_done, None)
        return

    def finished(p, result):
        try:
            p.wait_finish(result)
        except GLib.Error:
            pass
        if p.get_successful() and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            on_done(out)
        else:
            tmp.unlink(missing_ok=True)
            on_done(None)

    proc.wait_async(None, finished)
