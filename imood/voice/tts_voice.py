"""Qwen3-TTS voice-clone streaming (FasterQwen3TTS) + stateful 24k->16k resampling."""
import time

import numpy as np

from imood.dialogue.segmenter import Segmenter


def load(tts_config, fast_config, ref_audio, ref_text):
    import torch
    from faster_qwen3_tts import FasterQwen3TTS
    timings = {}
    started = time.perf_counter()
    tts = FasterQwen3TTS.from_pretrained(tts_config["model"], device="cuda", dtype=getattr(torch, tts_config["dtype"]),
                                         attn_implementation=tts_config["attn_implementation"], max_seq_len=fast_config["max_seq_len"])
    tts.warmup(prefill_len=100)
    timings["tts_load_and_graph_capture_s"] = round(time.perf_counter() - started, 2)
    started = time.perf_counter()   # cache the ref prompt + warm the streaming decode path (outputs discarded)
    clone_kwargs = dict(language=tts_config["language"], ref_audio=ref_audio, ref_text=ref_text,
                        xvec_only=fast_config["xvec_only"], append_silence=fast_config["append_silence"])
    tts.generate_voice_clone(text="嗯。", **clone_kwargs)
    for _ in tts.generate_voice_clone_streaming(text="你好呀，我在呢。", chunk_size=fast_config["chunk_size"], **clone_kwargs):
        pass
    timings["tts_warmup_s"] = round(time.perf_counter() - started, 2)
    return tts, timings


def stream_segments(tts, generate_kwargs, text_queue, resampler, emit_audio, stats, mark_timestamp, raw_24k_chunks, sent_16k_chunks):
    """Consume text segments from text_queue until None; for every TTS chunk (float32 24 kHz, not 20 ms) resample to 16 kHz
    and call emit_audio(bytes of float32-LE). raw_24k_chunks / sent_16k_chunks collect the audio for the saved wavs."""
    while True:
        segment = text_queue.get()
        if segment is None:
            break
        if not Segmenter.speakable(segment):
            continue
        stats["tts_segments"] += 1
        for chunk, sample_rate, _elapsed in tts.generate_voice_clone_streaming(text=segment.strip(), **generate_kwargs):
            samples_24k = np.asarray(chunk, dtype=np.float32).flatten()
            raw_24k_chunks.append(samples_24k); stats["tts_chunks"] += 1
            mark_timestamp("tts_first_chunk")
            samples_16k = resampler.resample_chunk(samples_24k)
            if len(samples_16k):
                sent_16k_chunks.append(samples_16k); emit_audio(samples_16k.astype("<f4").tobytes())
    samples_16k = resampler.resample_chunk(np.zeros(0, np.float32), last=True)
    if len(samples_16k):
        sent_16k_chunks.append(samples_16k); emit_audio(samples_16k.astype("<f4").tobytes())
