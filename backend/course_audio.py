"""Bounded-memory transcription of temporary 16 kHz mono PCM audio."""
import wave


def transcribe_audio(model, path, video_index, chunk_seconds=180):
    import numpy as np

    sections = []
    with wave.open(str(path), "rb") as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Expected 16 kHz mono PCM16 audio")
        total = audio.getnframes()
        frames_per_chunk = int(chunk_seconds * 16000)
        if frames_per_chunk <= 0:
            raise ValueError("Invalid audio chunk size")
        offset = 0
        while offset < total:
            samples = np.frombuffer(audio.readframes(frames_per_chunk), dtype="<i2").astype(np.float32)
            samples /= 32768.0
            segments, _ = model.transcribe(samples, language="ru", beam_size=3, vad_filter=True)
            block, start = [], None
            for segment in segments:
                text = segment.text.strip()
                if text:
                    if start is None:
                        start = int(offset / 16000 + segment.start)
                    block.append(text)
                    if sum(len(part)+1 for part in block) >= 900:
                        sections.append({"locator": f"video {video_index} {start//60}:{start%60:02d}", "text": " ".join(block)})
                        block, start = [], None
            if block:
                sections.append({"locator": f"video {video_index} {start//60}:{start%60:02d}", "text": " ".join(block)})
            offset += len(samples)
    if not sections:
        raise ValueError("No speech recognized; not marking video imported")
    return sections, total / 16000
