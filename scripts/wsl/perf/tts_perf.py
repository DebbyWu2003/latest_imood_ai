"""Controlled Qwen3-TTS performance investigation (measurement only; no model / quality changes).

A. VRAM at each stage (whole GPU via nvidia-smi + torch allocator view)
B. Runtime verification: device / dtype per component, attention backend, SDPA kernels (profiler),
   CPU fallback, GPU + CPU utilisation during synthesis, generation parameters
C. Fixed-text benchmark: warm-up + N measured runs, same seed, same ref prompt
D. Streaming: installed qwen-tts has no streaming generation API -> measured via runtime hooks only:
   when the first codec frames exist and how long decoding a first chunk takes (lower bound for any
   streaming implementation of this runtime)

Nothing in site-packages is edited: timing is done by wrapping bound methods on the loaded instance and
registering forward hooks.
"""
import argparse, json, os, subprocess, sys, threading, time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXED_TEXT = "今天嘛，就是平平淡淡过日子呀。倒是你，有没有想我啊？我可是很想你呢。"
SEED = 1234


# ---------------------------------------------------------------- samplers
def smi():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu,clocks.sm,power.draw",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    mem, util, clk, pwr = [x.strip() for x in out.split(",")]
    return int(mem), int(util), int(clk), float(pwr)


def cpu_times():
    with open("/proc/self/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    proc = (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")      # utime + stime of this process
    with open("/proc/stat") as f:
        cores = [list(map(int, l.split()[1:8])) for l in f if l.startswith("cpu") and l[3] != " "]
    return proc, cores


class Sampler(threading.Thread):
    """Samples whole-GPU memory/util/clock and this process' CPU usage while `active`."""

    def __init__(self, interval=0.1):
        super().__init__(daemon=True)
        self.interval, self.active, self.stop_flag = interval, False, False
        self.reset()

    def reset(self):
        self.mem, self.util, self.clk, self.pwr = [], [], [], []

    def run(self):
        while not self.stop_flag:
            if self.active:
                try:
                    m, u, c, p = smi()
                    self.mem.append(m); self.util.append(u); self.clk.append(c); self.pwr.append(p)
                except Exception:
                    pass
            time.sleep(self.interval)

    def summary(self):
        avg = lambda v: round(sum(v) / len(v), 1) if v else None
        return {"gpu_mem_peak_mib": max(self.mem) if self.mem else None, "gpu_util_avg_pct": avg(self.util),
                "gpu_util_max_pct": max(self.util) if self.util else None, "sm_clock_avg_mhz": avg(self.clk),
                "power_avg_w": avg(self.pwr), "samples": len(self.mem)}


class CpuMeter:
    def __enter__(self):
        self.t0 = time.perf_counter(); self.p0, self.c0 = cpu_times(); return self

    def __exit__(self, *a):
        wall = time.perf_counter() - self.t0
        p1, c1 = cpu_times()
        busy = []
        for a0, a1 in zip(self.c0, c1):
            tot = sum(a1) - sum(a0); idle = (a1[3] + a1[4]) - (a0[3] + a0[4])
            busy.append(round(100 * (tot - idle) / tot, 1) if tot else 0.0)
        self.result = {"process_cpu_pct": round(100 * (p1 - self.p0) / wall, 1),   # 100 = one full core
                       "busiest_core_pct": max(busy), "cores": len(busy),
                       "system_avg_pct": round(sum(busy) / len(busy), 1)}


# ---------------------------------------------------------------- helpers
def torch_mem(torch):
    return {"allocated_mib": round(torch.cuda.memory_allocated() / 2**20),
            "reserved_mib": round(torch.cuda.memory_reserved() / 2**20)}


def module_report(mod):
    dev, dt, nbytes, n = Counter(), Counter(), 0, 0
    for p in list(mod.parameters()) + list(mod.buffers()):
        dev[str(p.device)] += p.numel(); dt[str(p.dtype)] += p.numel()
        nbytes += p.numel() * p.element_size(); n += p.numel()
    return {"params_M": round(n / 1e6, 1), "size_mib": round(nbytes / 2**20),
            "devices": dict(dev), "dtypes": {k: round(v / 1e6, 1) for k, v in dt.items()}}


def attn_impl(cfg):
    return getattr(cfg, "_attn_implementation", None)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--external", default="{}", help="JSON with stage-1/2 numbers measured from Windows")
    args = ap.parse_args()

    out_dir = ROOT / "outputs/perf"; out_dir.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / "config/voice.json").read_text(encoding="utf-8"))
    ref_text = (ROOT / cfg["tts"]["ref_text_file"]).read_text(encoding="utf-8").strip()
    ext_file = out_dir / "_ext.json"   # written by scripts/windows/perf_vram_stages.ps1 (stages 1-2)
    ext = json.loads(ext_file.read_text(encoding="utf-8-sig")) if ext_file.exists() else json.loads(args.external)
    rep = {"fixed_text": FIXED_TEXT, "seed": SEED, "external": ext}

    sampler = Sampler(); sampler.start()
    stages = {}

    def stage(name, **extra):
        m, u, c, p = smi()
        stages[name] = {"gpu_mem_used_mib": m, **extra}
        print("STAGE", name, json.dumps(stages[name]), flush=True)

    import torch
    stage("3a_before_python_models")

    # ---- A.3 Faster-Whisper
    from faster_whisper import WhisperModel
    a = cfg["asr"]; t = time.perf_counter()
    asr = WhisperModel(a["model"], device=a["device"], compute_type=a["compute_type"])
    stage("3_after_whisper_load", load_s=round(time.perf_counter() - t, 2))

    # ---- A.4 Qwen3-TTS
    from qwen_tts import Qwen3TTSModel
    c = cfg["tts"]; t = time.perf_counter()
    tts = Qwen3TTSModel.from_pretrained(c["model"], device_map=c["device"], dtype=getattr(torch, c["dtype"]),
                                        attn_implementation=c["attn_implementation"])
    torch.cuda.synchronize()
    stage("4_after_tts_load", load_s=round(time.perf_counter() - t, 2), torch=torch_mem(torch))

    t = time.perf_counter()
    prompt = tts.create_voice_clone_prompt(ref_audio=str(ROOT / c["ref_audio"]), ref_text=ref_text,
                                           x_vector_only_mode=False)
    torch.cuda.synchronize()
    rep["voice_prompt_s"] = round(time.perf_counter() - t, 3)
    rep["ref_code_frames"] = int(prompt[0].ref_code.shape[0]) if prompt[0].ref_code is not None else None

    # ---- B. runtime verification (static)
    m = tts.model
    comps = {"talker (28L, excl. code_predictor)": None, "talker.code_predictor (5L)": m.talker.code_predictor}
    talker_only = {n: p for n, p in m.talker.named_parameters() if not n.startswith("code_predictor.")}
    dev, dt, nb = Counter(), Counter(), 0
    for p in talker_only.values():
        dev[str(p.device)] += p.numel(); dt[str(p.dtype)] += p.numel(); nb += p.numel() * p.element_size()
    comp_rep = {"talker (28L, excl. code_predictor)": {"params_M": round(sum(dev.values()) / 1e6, 1),
                "size_mib": round(nb / 2**20), "devices": dict(dev),
                "dtypes": {k: round(v / 1e6, 1) for k, v in dt.items()}},
                "talker.code_predictor (5L)": module_report(m.talker.code_predictor)}
    for name in ("speaker_encoder",):
        if getattr(m, name, None) is not None:
            comp_rep[name] = module_report(getattr(m, name))
    st = m.speech_tokenizer
    st_mod = st if isinstance(st, torch.nn.Module) else getattr(st, "model", None)
    if st_mod is not None:
        comp_rep["speech_tokenizer (12Hz codec)"] = module_report(st_mod)
    comp_rep["WHOLE Qwen3TTSForConditionalGeneration"] = module_report(m)
    rep["B_components"] = comp_rep
    rep["B_attention"] = {
        "top_config": attn_impl(m.config),
        "talker": attn_impl(m.talker.config),
        "code_predictor": attn_impl(m.talker.code_predictor.config),
        "speech_tokenizer_decoder": attn_impl(getattr(getattr(st_mod, "decoder", None), "config", None))
        if st_mod is not None else None,
        "torch_sdpa_backends_enabled": {"flash": torch.backends.cuda.flash_sdp_enabled(),
                                        "mem_efficient": torch.backends.cuda.mem_efficient_sdp_enabled(),
                                        "math": torch.backends.cuda.math_sdp_enabled(),
                                        "cudnn": torch.backends.cuda.cudnn_sdp_enabled()},
        "flash_attn_package_installed": __import__("importlib").util.find_spec("flash_attn") is not None,
    }
    rep["B_generation_params"] = {"model.generate_config (used as defaults)": tts.generate_defaults,
                                  "talker_frame_rate_hz": 12.5,
                                  "code_groups_per_frame": m.config.talker_config.num_code_groups}
    rep["B_torch"] = {"torch": torch.__version__, "cuda": torch.version.cuda, "cudnn": torch.backends.cudnn.version(),
                      "tf32_matmul": torch.backends.cuda.matmul.allow_tf32, "device": torch.cuda.get_device_name(0)}
    print("B", json.dumps({k: rep[k] for k in ("B_components", "B_attention")}, ensure_ascii=False), flush=True)

    # ---- instrumentation: wrap bound methods on this instance only
    timers = defaultdict(float); counts = Counter(); marks = {}; originals = {}

    def wrap(obj, attr, key):
        orig = getattr(obj, attr)
        originals[key] = orig
        def w(*a, **k):
            torch.cuda.synchronize(); t0 = time.perf_counter()
            r = orig(*a, **k)
            torch.cuda.synchronize(); timers[key] += time.perf_counter() - t0; counts[key] += 1
            return r
        setattr(obj, attr, w)

    wrap(m, "generate", "codec_generate")               # talker + code predictor loop
    wrap(m.speech_tokenizer, "decode", "codec_decode")  # codes -> waveform

    def talker_hook(mod, inp, kw, out):
        counts["talker_forward"] += 1
        if counts["talker_forward"] == 1:
            marks["prefill_done"] = time.perf_counter()
        elif counts["talker_forward"] == 2:
            marks["first_frame_done"] = time.perf_counter()
        marks["last_forward"] = time.perf_counter()
        marks.setdefault("frame_times", []).append(time.perf_counter())

    def cp_hook(mod, inp, out):
        counts["code_predictor_forward"] += 1

    m.talker.register_forward_hook(talker_hook, with_kwargs=True)
    m.talker.code_predictor.register_forward_hook(cp_hook)

    import soundfile as sf

    def synth(tag, save=None):
        timers.clear(); counts.clear(); marks.clear()
        torch.manual_seed(SEED); torch.cuda.manual_seed_all(SEED)
        torch.cuda.reset_peak_memory_stats()
        sampler.reset(); sampler.active = True
        with CpuMeter() as cm:
            t0 = time.perf_counter()
            wavs, sr = tts.generate_voice_clone(text=FIXED_TEXT, language=c["language"], voice_clone_prompt=prompt)
            torch.cuda.synchronize()
            total = time.perf_counter() - t0
        sampler.active = False
        audio_s = len(wavs[0]) / sr
        frames = max(counts["talker_forward"] - 1, 0)
        r = {"tag": tag, "tts_s": round(total, 3), "audio_s": round(audio_s, 3), "rtf": round(total / audio_s, 3),
             "codec_generate_s": round(timers["codec_generate"], 3), "codec_decode_s": round(timers["codec_decode"], 3),
             "other_s": round(total - timers["codec_generate"] - timers["codec_decode"], 3),
             "frames_generated": frames, "talker_forwards": counts["talker_forward"],
             "code_predictor_forwards": counts["code_predictor_forward"],
             "ms_per_frame": round(1000 * timers["codec_generate"] / frames, 1) if frames else None,
             "realtime_ms_per_frame": 80.0,
             "prefill_s": round(marks["prefill_done"] - t0, 3) if "prefill_done" in marks else None,
             "first_frame_s": round(marks["first_frame_done"] - t0, 3) if "first_frame_done" in marks else None,
             "torch_peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20),
             "torch_reserved_mib": round(torch.cuda.memory_reserved() / 2**20),
             **sampler.summary(), "cpu": cm.result}
        if save:
            sf.write(str(save), wavs[0], sr); r["wav"] = str(Path(save).relative_to(ROOT))
        print("RUN", json.dumps(r), flush=True)
        return r, wavs[0], sr, list(marks.get("frame_times", [])), t0

    # ---- C. warm-up + measured runs (A.5 peak comes from these)
    rep["C_warmup"] = synth("warmup")[0]
    runs = []
    for i in range(1, args.runs + 1):
        r, wav, sr, ftimes, t0 = synth(f"run{i}", save=out_dir / f"fixed_run{i}.wav")
        runs.append(r)
    rep["C_runs"] = runs
    stage("6_after_tts_generation", torch=torch_mem(torch))
    stages["5_during_tts_generation_peak"] = {"gpu_mem_used_mib": max(r["gpu_mem_peak_mib"] or 0 for r in runs)}

    # ---- D. streaming (not supported by qwen-tts 0.1.1 -> measured lower bound)
    ftimes_rel = [round(x - t0, 3) for x in ftimes]      # talker forward end times of last run (0 = prefill)
    codes_frames = r["frames_generated"]
    chunk_frames = [6, 12]                               # 0.48 s and 0.96 s of audio
    dec = {}
    # decode time of a first chunk: decode the ref prompt + first k generated frames, like the wrapper does
    torch.manual_seed(SEED)
    talker_codes, _ = originals["codec_generate"](  # original (unwrapped) generate
        input_ids=tts._tokenize_texts([tts._build_assistant_text(FIXED_TEXT)]),
        ref_ids=[tts._tokenize_texts([tts._build_ref_text(ref_text)])[0]],
        voice_clone_prompt=tts._prompt_items_to_voice_clone_prompt(prompt), languages=[c["language"]],
        non_streaming_mode=False, **tts._merge_generate_kwargs())
    ref_code = prompt[0].ref_code.to(talker_codes[0].device)
    orig_decode = originals["codec_decode"]
    for k in chunk_frames:
        codes = torch.cat([ref_code, talker_codes[0][:k]], dim=0)
        times = []
        for _ in range(3):
            torch.cuda.synchronize(); t = time.perf_counter()
            orig_decode([{"audio_codes": codes}]); torch.cuda.synchronize()
            times.append(time.perf_counter() - t)
        dec[k] = round(min(times), 3)
    first_frame_at = ftimes_rel[1] if len(ftimes_rel) > 1 else None
    rep["D_streaming"] = {
        "official_streaming_api_in_installed_version": False,
        "evidence": "qwen_tts 0.1.1 docstring: non_streaming_mode=False 'only simulates streaming text input ... "
                    "rather than enabling true streaming input or streaming generation'; generate_voice_clone decodes "
                    "only after all codes exist; model.generate() does not forward a streamer. Upstream main is also "
                    "0.1.1; vLLM-Omni: offline inference only, streaming 'later'.",
        "measured_on_last_run": {
            "prefill_s": ftimes_rel[0] if ftimes_rel else None,
            "first_codec_frame_s": first_frame_at,
            f"frame_{chunk_frames[0]}_ready_s": ftimes_rel[chunk_frames[0]] if len(ftimes_rel) > chunk_frames[0] else None,
            f"frame_{chunk_frames[1]}_ready_s": ftimes_rel[chunk_frames[1]] if len(ftimes_rel) > chunk_frames[1] else None,
            "decode_first_chunk_s": {f"{k}_frames({k*0.08:.2f}s audio)": v for k, v in dec.items()},
        },
        "estimated_time_to_first_audio_s": {
            f"{k}_frames": round(ftimes_rel[k] + dec[k], 3) for k in chunk_frames if len(ftimes_rel) > k},
        "can_play_continuously": runs[-1]["ms_per_frame"] is not None and runs[-1]["ms_per_frame"] <= 80.0,
        "note": "Estimated lower bound for a streaming implementation on this exact runtime; not an official streaming "
                "result, and no streamed WAV can be produced with the installed package.",
    }
    print("D", json.dumps(rep["D_streaming"]), flush=True)

    # ---- B. profiler on one warm synthesis: kernels, attention backend, CPU/GPU busy time
    from torch.profiler import profile, ProfilerActivity
    torch.manual_seed(SEED)
    with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as prof:
        t = time.perf_counter()
        tts.generate_voice_clone(text=FIXED_TEXT, language=c["language"], voice_clone_prompt=prompt)
        torch.cuda.synchronize(); wall = time.perf_counter() - t
    ka = prof.key_averages()
    dev_attr = "self_device_time_total" if hasattr(ka[0], "self_device_time_total") else "self_cuda_time_total"
    kernels = [(e.key, getattr(e, dev_attr), e.count) for e in ka if getattr(e, dev_attr) > 0 and e.device_type.name == "CUDA"]
    gpu_busy_us = sum(k[1] for k in kernels)
    kernels.sort(key=lambda x: -x[1])
    attn = {"flash": 0, "mem_efficient": 0, "math_or_other": 0}
    for e in ka:
        if e.key in ("aten::_scaled_dot_product_flash_attention", "aten::_flash_attention_forward"): attn["flash"] += e.count
        elif e.key in ("aten::_scaled_dot_product_efficient_attention", "aten::_efficient_attention_forward"): attn["mem_efficient"] += e.count
        elif e.key in ("aten::_scaled_dot_product_attention_math",): attn["math_or_other"] += e.count
    cpu_ops = {e.key: e.count for e in ka}
    rt = {"cudaLaunchKernel": cpu_ops.get("cudaLaunchKernel", 0),
          "sync_points (aten::item/_local_scalar_dense)": cpu_ops.get("aten::_local_scalar_dense", 0),
          "cudaStreamSynchronize": cpu_ops.get("cudaStreamSynchronize", 0),
          "cudaMemcpyAsync": cpu_ops.get("cudaMemcpyAsync", 0)}
    # ops that ran with real compute but no GPU time (possible CPU fallback)
    heavy_cpu = sorted([(e.key, round(e.self_cpu_time_total / 1000, 1), e.count) for e in ka
                        if e.key.startswith("aten::") and getattr(e, dev_attr) == 0 and e.self_cpu_time_total > 20_000],
                       key=lambda x: -x[1])[:10]
    rep["B_profiler"] = {"wall_s": round(wall, 3), "gpu_kernel_busy_s": round(gpu_busy_us / 1e6, 3),
                         "gpu_busy_fraction": round(gpu_busy_us / 1e6 / wall, 3),
                         "sdpa_calls_by_backend": attn, "runtime_calls": rt,
                         "top_cuda_kernels_ms": [(k[:90], round(t / 1000, 1), n) for k, t, n in kernels[:15]],
                         "aten_ops_cpu_only_over_20ms": heavy_cpu}
    print("PROF", json.dumps(rep["B_profiler"]), flush=True)

    # ---- A.7 (informational): what unloading Whisper would free
    del asr; import gc; gc.collect(); time.sleep(1)
    stage("7_after_unloading_whisper_informational")

    rep["A_stages"] = stages
    sampler.stop_flag = True
    (out_dir / "tts_perf.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print("DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
