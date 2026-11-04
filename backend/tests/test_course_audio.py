import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from course_audio import transcribe_audio


class AudioTests(unittest.TestCase):
    def make_audio(self, path, channels=1):
        with wave.open(str(path), "wb") as audio:
            audio.setparams((channels, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 40000 * channels)

    def test_chunks_preserve_source_timestamps_and_bound_input(self):
        calls = []

        class Model:
            def transcribe(self, audio, **kwargs):
                calls.append(len(audio))
                return iter([SimpleNamespace(start=0, text=" text ")]), None

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"audio.wav"
            self.make_audio(path)
            sections, duration = transcribe_audio(Model(), path, 2, chunk_seconds=1)
        self.assertEqual(calls, [16000, 16000, 8000])
        self.assertEqual(duration, 2.5)
        self.assertEqual([s["locator"] for s in sections], ["video 2 0:00", "video 2 0:01", "video 2 0:02"])

    def test_invalid_audio_is_not_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"audio.wav"
            self.make_audio(path, channels=2)
            with self.assertRaises(ValueError):
                transcribe_audio(None, path, 1)

    def test_silence_is_not_claimed_as_learned(self):
        model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter([]), None))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"audio.wav"
            self.make_audio(path)
            with self.assertRaises(ValueError):
                transcribe_audio(model, path, 1)
