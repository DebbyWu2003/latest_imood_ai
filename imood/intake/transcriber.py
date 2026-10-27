"""Speech recognition (faster-whisper). Moved unchanged from the legacy voice service."""
import io
import time


def load(a):
    from faster_whisper import WhisperModel
    t = time.perf_counter()
    model = WhisperModel(a["model"], device=a["device"], compute_type=a["compute_type"])
    return model, round(time.perf_counter() - t, 2)


def transcribe(model, wav_bytes, a):
    """-> (text, input_audio_seconds)"""
    segs, info = model.transcribe(io.BytesIO(wav_bytes), language=a["language"], beam_size=a["beam_size"], vad_filter=a["vad_filter"])
    return "".join(s.text for s in segs).strip(), info.duration
