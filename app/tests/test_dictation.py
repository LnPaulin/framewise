import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import GLib

from framewise import dictation, speech


class CleanTests(unittest.TestCase):
    def test_phantoms_dropped(self):
        for t in ("Thank you.", " thank   you. ", "you", "", None):
            self.assertEqual(dictation.clean(t), "")

    def test_spaces_tidied(self):
        self.assertEqual(dictation.clean("  Hello,\n world. "), "Hello, world.")


def speech_wav(path):
    """A spoken sentence (espeak), 16 kHz mono, or None when espeak isn't there."""
    import ctypes
    import wave
    try:
        lib = ctypes.CDLL("libespeak-ng.so.1")
    except OSError:
        return None
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int,
                                     ctypes.c_void_p)
    samples = []

    def cb(wav, n, _events):
        if wav and n > 0:
            samples.extend(wav[:n])
        return 0
    keep = callback_type(cb)
    rate = lib.espeak_Initialize(2, 0, None, 0)
    lib.espeak_SetSynthCallback(keep)
    lib.espeak_SetVoiceByName(b"en")
    text = b"Hello, this is a dictation test.\0"
    lib.espeak_Synth(text, len(text), 0, 0, 0, 0x1, None, None)
    lib.espeak_Synchronize()
    raw = path.with_suffix(".raw.wav")
    with wave.open(str(raw), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    # Half a second of quiet, the sentence, then 4 s of quiet (a pause that finishes).
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", "adelay=500,apad=pad_dur=4",
                    "-ar", "16000", "-ac", "1", str(path)], check=True)
    raw.unlink()
    return path


@unittest.skipUnless(speech.is_ready(), "speech model not installed")
class DictationFlowTests(unittest.TestCase):
    """The whole flow with a sound file instead of the microphone, and the
    typing captured instead of sent to a window."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.wav = speech_wav(self.dir / "say.wav")
        if self.wav is None:
            self.skipTest("espeak not installed")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_spoken_text_is_typed(self):
        loop = GLib.MainLoop()
        typed, states = [], []
        shell = mock.Mock(has_dictation=True)
        shell.type_text.side_effect = lambda t: (typed.append(t), loop.quit(), True)[2]
        shell.show_dictation.side_effect = states.append
        app = mock.Mock()
        app.session.shell = shell
        app.notify.side_effect = lambda *a: (typed.append(("notify",) + a), loop.quit())
        d = dictation.Dictation(app)
        # Played in real time, as a microphone would deliver it.
        d.SOURCE = f'filesrc location="{self.wav}" ! wavparse ! identity sync=true'
        with mock.patch.object(dictation.settings, "get",
                               side_effect=lambda k: "en" if k == "dictation_language" else ""):
            d.start()
            GLib.timeout_add_seconds(60, loop.quit)
            loop.run()
        d.shutdown()
        self.assertEqual(states, ["listening", "writing"])
        self.assertEqual(len(typed), 1, typed)
        self.assertIn("dictation test", typed[0].lower())
        self.assertTrue(typed[0].endswith(" "))
        self.assertEqual(d.state, d.IDLE)
        app.hold.assert_called_once()
        app.release.assert_called_once()


class NoModelTests(unittest.TestCase):
    def test_asks_to_download(self):
        app = mock.Mock()
        with mock.patch.object(speech, "is_ready", return_value=False):
            d = dictation.Dictation(app)
            d.start()
        self.assertEqual(d.state, d.IDLE)
        app.send_notification.assert_called_once()
        app.hold.assert_not_called()


if __name__ == "__main__":
    unittest.main()
