"""Runs speech recognition in framewise's own Python environment.

Started by speech.py with the private venv's Python, never imported by the
app (it needs faster-whisper, which only exists in that venv). Talks back on
stdout, one JSON object per line:

  {"progress": 0.42}
  {"words": [{"word": "um", "start": 1.2, "end": 1.5}, ...], "language": "en"}
  {"error": "..."}

Usage:
  python speech_worker.py download MODEL_DIR MODEL_NAME
  python speech_worker.py transcribe MODEL_DIR AUDIO.wav [LANGUAGE]
  python speech_worker.py serve MODEL_DIR

serve (dictation) loads the model once, says {"ready": true}, then reads one
request per line on stdin, {"wav": PATH, "language": "fr" or ""}, and answers
each with {"text": "...", "language": "fr"} (or {"error": ...}).
"""

import json
import sys

# Whisper tends to tidy fillers away; a prompt full of them makes it keep
# writing them down as they are said.
PROMPT = ("Umm, let me think like, hmm... Okay, here's what I'm, like, thinking. "
          "Uh, so, erm, I mean, um, yeah. Mm.")


def say(**kw):
    print(json.dumps(kw), flush=True)


def download(model_dir, name):
    from faster_whisper import download_model
    path = download_model(name, output_dir=model_dir)
    say(done=True, path=path)


def read_wav(path):
    """16 kHz mono 16-bit WAV (made by the app with ffmpeg) -> float32 samples.

    Reading it here avoids faster-whisper's own decoder, which depends on the
    exact PyAV version installed.
    """
    import wave

    import numpy as np
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(model_dir, audio, language=None):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_dir, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        read_wav(audio), language=language or None, word_timestamps=True, initial_prompt=PROMPT,
        condition_on_previous_text=False, vad_filter=False, beam_size=5)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"word": w.word.strip(), "start": round(w.start, 3),
                          "end": round(w.end, 3), "p": round(w.probability, 3)})
        if info.duration:
            say(progress=min(1.0, seg.end / info.duration))
    say(words=words, language=info.language)


def dictate(model, audio, language=None):
    """Plain text for dictation: punctuated, no word timings, silence skipped."""
    segments, info = model.transcribe(
        read_wav(audio), language=language or None, beam_size=5, vad_filter=True,
        condition_on_previous_text=False)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text, info.language


def serve(model_dir):
    import os

    from faster_whisper import WhisperModel
    # Dictation is waited on: more threads answer sooner (about twice as fast
    # as the default 4), leaving a couple of cores for everything else.
    threads = min(8, max(4, (os.cpu_count() or 4) - 2))
    model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=threads)
    say(ready=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            text, language = dictate(model, req["wav"], req.get("language"))
            say(text=text, language=language)
        except Exception as e:  # one bad request must not end the session
            say(error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "download":
            download(sys.argv[2], sys.argv[3])
        elif sys.argv[1] == "serve":
            serve(sys.argv[2])
        elif sys.argv[1] == "transcribe":
            transcribe(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    except Exception as e:  # report anything to the app instead of a traceback
        say(error=f"{type(e).__name__}: {e}")
        sys.exit(1)
