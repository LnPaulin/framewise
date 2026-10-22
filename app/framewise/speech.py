"""Optional speech recognition, used to find filler words (um, uh, hmm...).

Nothing here is part of the installer. On first use the app creates a
private Python environment under ~/.local/share/framewise/speech, installs
faster-whisper into it and downloads a Whisper model. Recognition runs on
this computer; no audio leaves it.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from gi.repository import Gio, GLib

from .state import DATA_DIR

SPEECH_DIR = DATA_DIR / "speech"
VENV = SPEECH_DIR / "venv"
PYTHON = VENV / "bin" / "python"
MODEL_NAME = "small"            # multilingual, ~460 MB; good word timing on a CPU
MODEL_DIR = SPEECH_DIR / f"model-{MODEL_NAME}"
READY_FLAG = SPEECH_DIR / "ready.json"
WORKER = Path(__file__).with_name("speech_worker.py")
DOWNLOAD_SIZE = "about 600 MB to download, 1 GB on disk"
# Pinned so every machine gets the same, tested set.
PACKAGES = ["faster-whisper==1.2.1", "ctranslate2==4.8.3"]

# Filler sounds, English and French. Matched on the whole word, lowercased,
# without punctuation: "Um," "Ummm" "Hmm..." "uh" "erm" "euh".
FILLER = re.compile(r"^(u+h*m+|u+m*h+|h+m+|m+h*m+|m+|e+r+m*|a+h+|e+h+|e+u+h+|h+e+u+)$")


def is_filler(word):
    return bool(FILLER.match(re.sub(r"[^\w]", "", word.lower())))


def can_install():
    """Python's venv module with pip bootstrapping (package python3-venv)."""
    try:
        import ensurepip  # noqa: F401
        return True
    except ImportError:
        return False


def is_ready():
    return READY_FLAG.exists() and PYTHON.exists() and MODEL_DIR.exists()


def _run_steps(steps, on_progress, on_done):
    """Runs commands one after another without blocking the app.

    steps: [(label, argv, weight)]. on_progress(label, fraction) and
    on_done(ok, message) are called on the main loop.
    """
    total = sum(w for _l, _a, w in steps)
    done_weight = [0.0]

    def run(i):
        if i == len(steps):
            on_done(True, "")
            return
        label, argv, weight = steps[i]
        on_progress(label, done_weight[0] / total)
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE
                                      | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            on_done(False, e.message)
            return
        out = []
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())

        def line(src, result):
            try:
                text, _n = src.read_line_finish_utf8(result)
            except GLib.Error:
                text = None
            if text is None:
                return
            out.append(text)
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, line)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, line)

        def finished(p, result):
            try:
                p.wait_finish(result)
            except GLib.Error:
                pass
            if not p.get_successful():
                on_done(False, "\n".join(out[-8:]) or f"{label} failed")
                return
            done_weight[0] += weight
            run(i + 1)

        proc.wait_async(None, finished)

    run(0)


def install_async(on_progress, on_done):
    """Creates the private environment and downloads the model (one time)."""
    SPEECH_DIR.mkdir(parents=True, exist_ok=True)
    pip = str(VENV / "bin" / "pip")
    steps = [
        ("Preparing", [sys.executable, "-m", "venv", "--clear", str(VENV)], 1),
        ("Installing speech recognition", [pip, "install", "--quiet", "--disable-pip-version-check",
                                           *PACKAGES], 4),
        ("Downloading the speech model", [str(PYTHON), str(WORKER), "download",
                                          str(MODEL_DIR), MODEL_NAME], 6),
    ]

    def done(ok, message):
        if ok:
            READY_FLAG.write_text(json.dumps({"model": MODEL_NAME}))
        on_done(ok, message)

    _run_steps(steps, on_progress, done)


def remove():
    """Deletes the speech software and model (frees the disk space)."""
    shutil.rmtree(SPEECH_DIR, ignore_errors=True)


def transcribe_async(take, track_index, out_json, on_progress, on_done):
    """Writes the words with their times to out_json. on_done(words or None, message)."""
    wav = Path(out_json).with_suffix(".wav")
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(take),
                        "-map", f"0:a:{track_index}", "-ac", "1", "-ar", "16000", str(wav)],
                       check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        on_done(None, f"Couldn't read the sound: {e}")
        return None
    proc = Gio.Subprocess.new([str(PYTHON), str(WORKER), "transcribe", str(MODEL_DIR), str(wav)],
                              Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
    stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
    result = {}

    def line(src, res):
        try:
            text, _n = src.read_line_finish_utf8(res)
        except GLib.Error:
            text = None
        if text is None:
            return
        try:
            msg = json.loads(text)
        except ValueError:
            msg = {}
        if "progress" in msg:
            on_progress(msg["progress"])
        result.update({k: v for k, v in msg.items() if k != "progress"})
        src.read_line_async(GLib.PRIORITY_DEFAULT, None, line)

    stream.read_line_async(GLib.PRIORITY_DEFAULT, None, line)

    def finished(p, res):
        try:
            p.wait_finish(res)
        except GLib.Error:
            pass
        wav.unlink(missing_ok=True)

        def report():
            if "words" in result:
                Path(out_json).write_text(json.dumps(result, indent=1))
                on_done(result["words"], "")
            else:
                on_done(None, result.get("error", "Speech recognition stopped"))
            return False

        # Let the last output lines arrive before reporting.
        GLib.timeout_add(150, report)

    proc.wait_async(None, finished)
    return proc


def filler_cuts(words, db=None, threshold=None, keep=0.08, window=0.02, long_gap=1.5):
    """Cut ranges for filler words, never reaching into the real words around them.

    Speech recognition times fillers loosely (often far too short), so the
    cut covers the gap between the neighbouring real words, which holds the
    filler and the hesitation around it, leaving `keep` seconds at each end.
    When that gap is long and loudness is known (db per `window` seconds),
    only the voiced stretch around the filler is cut, so a deliberate pause
    after an "um" stays.
    """
    cuts = []
    i = 0
    while i < len(words):
        if not is_filler(words[i]["word"]):
            i += 1
            continue
        j = i
        while j + 1 < len(words) and is_filler(words[j + 1]["word"]):
            j += 1  # "erm, erm" is one hesitation
        prev_end = words[i - 1]["end"] if i > 0 else 0.0
        next_start = words[j + 1]["start"] if j + 1 < len(words) else words[j]["end"] + 0.5
        a, b = prev_end + keep, next_start - keep
        if b - a > long_gap and db is not None and threshold is not None:
            lo = int(prev_end / window)
            hi = min(len(db), int(next_start / window))
            voiced = [k for k in range(lo, hi) if db[k] > threshold]
            if voiced:
                a = max(a, voiced[0] * window - 0.1)
                b = min(b, (voiced[-1] + 1) * window + 0.1)
        if b - a >= 0.08:
            cuts.append((round(a, 3), round(b, 3)))
        i = j + 1
    return cuts
