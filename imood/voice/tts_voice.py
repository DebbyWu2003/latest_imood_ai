"""Qwen3-TTS voice-clone streaming (FasterQwen3TTS) + stateful 24k->16k resampling. Moved unchanged from the legacy voice service."""
import time

import numpy as np

from imood.dialogue.segmenter import Segmenter


def load(c, tf, ref_audio, ref_text):
    import torch
    from faster_qwen3_tts import FasterQwen3TTS
    s = {}
    t = time.perf_counter()
    tts = FasterQwen3TTS.from_pretrained(c["model"], device="cuda", dtype=getattr(torch, c["dtype"]),
                                         attn_implementation=c["attn_implementation"], max_seq_len=tf["max_seq_len"])
    tts.warmup(prefill_len=100)
    s["tts_load_and_graph_capture_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()   # cache the ref prompt + warm the streaming decode path (outputs discarded)
    kw = dict(language=c["language"], ref_audio=ref_audio, ref_text=ref_text,
              xvec_only=tf["xvec_only"], append_silence=tf["append_silence"])
    tts.generate_voice_clone(text="嗯。", **kw)
    for _ in tts.generate_voice_clone_streaming(text="你好呀，我在呢。", chunk_size=tf["chunk_size"], **kw):
        pass
    s["tts_warmup_s"] = round(time.perf_counter() - t, 2)
    return tts, s


def stream_segments(tts, tts_kw, text_q, rs, emit, stat, mark, raw24, sent16):
    """Consume text segments from text_q until None; for every TTS chunk (float32 24 kHz, not 20 ms) resample to 16 kHz
    and call emit(bytes of float32-LE). raw24 / sent16 collect the audio for the saved wavs."""
    while True:
        seg = text_q.get()
        if seg is None:
            break
        if not Segmenter.speakable(seg):
            continue
        stat["tts_segments"] += 1
        for chunk, sr, _t in tts.generate_voice_clone_streaming(text=seg.strip(), **tts_kw):
            x = np.asarray(chunk, dtype=np.float32).flatten()
            raw24.append(x); stat["tts_chunks"] += 1
            mark("tts_first_chunk")
            y = rs.resample_chunk(x)
            if len(y):
                sent16.append(y); emit(y.astype("<f4").tobytes())
    y = rs.resample_chunk(np.zeros(0, np.float32), last=True)
    if len(y):
        sent16.append(y); emit(y.astype("<f4").tobytes())
