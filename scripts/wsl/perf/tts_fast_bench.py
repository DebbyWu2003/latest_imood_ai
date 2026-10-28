"""Benchmark the community-optimized Qwen3-TTS backend (faster-qwen3-tts, CUDA graphs + StaticCache)
under exactly the conditions of tts_perf.py (the original qwen-tts 0.1.1 baseline):
same model, ref.wav, ref_text.txt, fixed reply text, seed, bf16 + sdpa, Faster-Whisper loaded in the same process.

Also scores voice quality for BOTH backends the same way:
  * speaker similarity: cosine between Qwen3-TTS speaker-encoder x-vectors of each output and ref.wav
  * intelligibility: Whisper large-v3 round-trip transcript -> character error rate vs the fixed text
"""
import json, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tts_perf import FIXED_TEXT, SEED, CpuMeter, Sampler, smi  # same text / seed / samplers as the baseline

ROOT = HERE.parents[2]
OUT = ROOT / "outputs/perf_fast"


def cer(ref, hyp):
    norm = lambda s: re.sub(r"[\W_]+", "", s)
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return round(d[-1] / max(len(r), 1), 4)


def stall_analysis(arrivals, durs):
    """Playback starts when chunk 0 arrives; returns total stall time and number of stalls."""
    play_end, stall, n = arrivals[0] + durs[0], 0.0, 0
    for t, d in zip(arrivals[1:], durs[1:]):
        if t > play_end:
            stall += t - play_end; n += 1; play_end = t
        play_end += d
    return round(stall, 3), n


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / "config/voice.json").read_text(encoding="utf-8"))
    c, a = cfg["tts"], cfg["asr"]
    ref_audio = str(ROOT / c["ref_audio"])
    ref_text = (ROOT / c["ref_text_file"]).read_text(encoding="utf-8").strip()
    gen_kw = dict(text=FIXED_TEXT, language=c["language"], ref_audio=ref_audio, ref_text=ref_text,
                  xvec_only=False, append_silence=False)   # ICL like the baseline; no added silence = upstream behaviour
    rep = {"backend": "faster-qwen3-tts 0.5.2 (torch backend, CUDA graphs + StaticCache)",
           "fixed_text": FIXED_TEXT, "seed": SEED, "gen_kwargs": {k: v for k, v in gen_kw.items() if k != "text"}}
    sampler = Sampler(); sampler.start()
    stages = {}
    def stage(name, **x):
        stages[name] = {"gpu_mem_used_mib": smi()[0], **x}; print("STAGE", name, json.dumps(stages[name]), flush=True)

    import torch, numpy as np, soundfile as sf
    import importlib.metadata as md
    rep["versions"] = {p: md.version(p) for p in ("faster-qwen3-tts", "qwen-tts-hf", "transformers", "torch", "faster-whisper")}
    stage("before_models")

    from faster_whisper import WhisperModel
    t = time.perf_counter()
    asr = WhisperModel(a["model"], device=a["device"], compute_type=a["compute_type"])
    stage("after_whisper_load", load_s=round(time.perf_counter() - t, 2))

    from faster_qwen3_tts import FasterQwen3TTS
    t = time.perf_counter()
    model = FasterQwen3TTS.from_pretrained(c["model"], device="cuda", dtype=getattr(torch, c["dtype"]),
                                           attn_implementation=c["attn_implementation"], max_seq_len=2048)
    torch.cuda.synchronize()
    stage("after_tts_load", load_s=round(time.perf_counter() - t, 2))
    t = time.perf_counter()
    model.warmup(prefill_len=100)                     # CUDA graph capture (one-time)
    torch.cuda.synchronize()
    stage("after_cuda_graph_capture", capture_s=round(time.perf_counter() - t, 2))

    # voice-clone prompt preparation (the official function that the wrapper calls and then caches)
    t = time.perf_counter()
    model.model.create_voice_clone_prompt(ref_audio=ref_audio, ref_text=ref_text, x_vector_only_mode=False)
    torch.cuda.synchronize(); rep["voice_prompt_s"] = round(time.perf_counter() - t, 3)

    def measured(fn):
        torch.manual_seed(SEED); torch.cuda.manual_seed_all(SEED)
        sampler.reset(); sampler.active = True
        with CpuMeter() as cm:
            t0 = time.perf_counter(); res = fn(t0); torch.cuda.synchronize(); total = time.perf_counter() - t0
        sampler.active = False
        return res, total, {**sampler.summary(), "cpu": cm.result}

    # ---------------- non-streaming
    runs = []
    for i in range(0, 4):                             # 0 = warm-up (includes first ref-prompt extraction)
        (wavs, sr), total, util = measured(lambda t0: model.generate_voice_clone(**gen_kw))
        wav = np.asarray(wavs[0]).flatten(); dur = len(wav) / sr
        r = {"tag": "warmup" if i == 0 else f"run{i}", "tts_s": round(total, 3), "audio_s": round(dur, 3),
             "rtf": round(total / dur, 3), "sample_rate": sr, **util}
        if i:
            path = OUT / f"fast_nonstream_run{i}.wav"; sf.write(str(path), wav, sr); r["wav"] = str(path.relative_to(ROOT))
            runs.append(r)
        print("NONSTREAM", json.dumps(r), flush=True)
    rep["nonstreaming"] = runs

    # ---------------- streaming (official generate_voice_clone_streaming)
    stream = {}
    for cs in (8, 4):
        res_list = []
        for i in range(0, 3):                         # 0 = warm-up
            def run(t0):
                arr, chunks, sr_ = [], [], None
                for chunk, sr_, timing in model.generate_voice_clone_streaming(chunk_size=cs, **gen_kw):
                    arr.append(time.perf_counter() - t0); chunks.append(np.asarray(chunk).flatten())
                return arr, chunks, sr_
            (arr, chunks, sr), total, util = measured(run)
            durs = [len(x) / sr for x in chunks]
            audio = np.concatenate(chunks); dur = len(audio) / sr
            stall_s, n_stall = stall_analysis(arr, durs)
            r = {"chunk_size": cs, "tag": "warmup" if i == 0 else f"run{i}", "ttfa_s": round(arr[0], 3),
                 "first_chunk_audio_s": round(durs[0], 3), "chunks": len(chunks), "tts_total_s": round(total, 3),
                 "audio_s": round(dur, 3), "rtf": round(total / dur, 3), "playback_stall_s": stall_s,
                 "playback_stalls": n_stall, "sample_rate": sr, **util}
            if i:
                path = OUT / f"fast_stream_cs{cs}_run{i}.wav"; sf.write(str(path), audio, sr); r["wav"] = str(path.relative_to(ROOT))
                res_list.append(r)
            print("STREAM", json.dumps(r), flush=True)
        stream[f"chunk_size_{cs}"] = res_list
    rep["streaming"] = stream
    stage("after_generation", torch_reserved_mib=round(torch.cuda.memory_reserved() / 2**20),
          torch_peak_allocated_mib=round(torch.cuda.max_memory_allocated() / 2**20))
    rep["stages"] = stages
    rep["gpu_mem_peak_mib"] = max([r["gpu_mem_peak_mib"] or 0 for r in runs] +
                                  [r["gpu_mem_peak_mib"] or 0 for v in stream.values() for r in v])

    # ---------------- quality, identical scoring for both backends
    def xvec(path):
        item = model.model.create_voice_clone_prompt(ref_audio=str(path), ref_text="", x_vector_only_mode=True)[0]
        return item.ref_spk_embedding.float().flatten()
    ref_vec = xvec(ref_audio)
    cands = {"ref.wav (self)": ref_audio,
             "BASELINE qwen-tts fixed_run1": ROOT / "outputs/perf/fixed_run1.wav",
             "BASELINE qwen-tts fixed_run2": ROOT / "outputs/perf/fixed_run2.wav",
             "BASELINE voice_test reply.wav": ROOT / "outputs/voice_test/reply.wav",
             "FAST non-stream run1": OUT / "fast_nonstream_run1.wav",
             "FAST non-stream run2": OUT / "fast_nonstream_run2.wav",
             "FAST stream cs8 run1": OUT / "fast_stream_cs8_run1.wav",
             "FAST stream cs4 run1": OUT / "fast_stream_cs4_run1.wav"}
    qual = {}
    for name, p in cands.items():
        if not Path(p).exists():
            continue
        v = xvec(p)
        segs, _ = asr.transcribe(str(p), language=a["language"], beam_size=a["beam_size"])
        txt = "".join(s.text for s in segs).strip()
        is_fixed = "fixed" in name or "FAST" in name
        qual[name] = {"speaker_cos_sim_to_ref": round(float(torch.nn.functional.cosine_similarity(v, ref_vec, dim=0)), 4),
                      "asr": txt, "cer_vs_fixed_text": cer(FIXED_TEXT, txt) if is_fixed else None,
                      "duration_s": round(sf.info(str(p)).duration, 2), "sample_rate": sf.info(str(p)).samplerate}
        print("QUALITY", name, json.dumps(qual[name], ensure_ascii=False), flush=True)
    rep["quality"] = qual

    sampler.stop_flag = True
    (OUT / "fast_bench.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print("DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
