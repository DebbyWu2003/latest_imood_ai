"""Speech recognition (faster-whisper)."""
import io
import time


def load(asr_config):
    from faster_whisper import WhisperModel
    started = time.perf_counter()
    model = WhisperModel(asr_config["model"], device=asr_config["device"], compute_type=asr_config["compute_type"])
    return model, round(time.perf_counter() - started, 2)


def transcribe(model, wav_bytes, asr_config):
    """-> (text, input_audio_seconds)"""
    segments, info = model.transcribe(io.BytesIO(wav_bytes), language=asr_config["language"], beam_size=asr_config["beam_size"],
                                      vad_filter=asr_config["vad_filter"])
    return "".join(segment.text for segment in segments).strip(), info.duration
