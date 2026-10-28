"""Step 9 voice pipeline test (no mic / WebRTC / LiveTalking):

    input.wav -> Faster-Whisper large-v3 -> llama.cpp Qwen3-14B (:8090) -> Qwen3-TTS 1.7B Base clone -> reply.wav

Model loading and the voice-clone prompt are built once and timed separately; the per-run
timings (ASR, LLM first token / done, TTS, end-to-end) exclude them.
Run through scripts/wsl/run_voice_pipeline_bench.sh (sets CUDA library paths for CTranslate2).
"""
import argparse, json, re, subprocess, sys, threading, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class GpuSampler(threading.Thread):
    """Samples whole-GPU memory.used via nvidia-smi (includes llama-server and any other process)."""

    def __init__(self, interval=0.2):
        super().__init__(daemon=True)
        self.interval, self.peak, self.stop_flag = interval, 0, False

    def now(self):
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout
        return int(out.split()[0])

    def run(self):
        while not self.stop_flag:
            try:
                self.peak = max(self.peak, self.now())
            except Exception:
                pass
            time.sleep(self.interval)


def load_cfg():
    cfg = json.loads((ROOT / "config/voice.json").read_text(encoding="utf-8"))
    cfg["persona"] = (ROOT / cfg["llm"]["persona_file"]).read_text(encoding="utf-8").strip()
    cfg["ref_text"] = (ROOT / cfg["tts"]["ref_text_file"]).read_text(encoding="utf-8").strip()
    return cfg


def llm_stream(cfg, user_text, t0):
    body = {"messages": [{"role": "system", "content": cfg["persona"]}, {"role": "user", "content": user_text}],
            "stream": True, "temperature": cfg["llm"]["temperature"], "top_p": cfg["llm"]["top_p"],
            "max_tokens": cfg["llm"]["max_tokens"]}
    req = urllib.request.Request(cfg["llm"]["url"], data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    first, text = None, ""
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
            if delta.strip() and first is None:
                first = time.perf_counter() - t0
            text += delta
    # Qwen3 may still emit an (empty) think block; never send it to TTS
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text, first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(ROOT / "assets/test.wav"))
    ap.add_argument("--out", default=None, help="default: <output_dir>/reply.wav")
    ap.add_argument("--runs", type=int, default=2, help="run 1 = cold (first CUDA kernels), later runs = warm")
    args = ap.parse_args()

    cfg = load_cfg()
    out_dir = ROOT / cfg["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_wav = Path(args.out) if args.out else out_dir / "reply.wav"

    import torch, soundfile as sf
    from faster_whisper import WhisperModel
    from qwen_tts import Qwen3TTSModel

    gpu = GpuSampler()
    base_vram = gpu.now()
    gpu.start()
    setup = {"vram_before_load_mib": base_vram}

    t = time.perf_counter()
    a = cfg["asr"]
    asr = WhisperModel(a["model"], device=a["device"], compute_type=a["compute_type"])
    setup["asr_load_s"] = round(time.perf_counter() - t, 2)
    setup["vram_after_asr_load_mib"] = gpu.now()

    t = time.perf_counter()
    c = cfg["tts"]
    tts = Qwen3TTSModel.from_pretrained(c["model"], device_map=c["device"], dtype=getattr(torch, c["dtype"]),
                                        attn_implementation=c["attn_implementation"])
    setup["tts_load_s"] = round(time.perf_counter() - t, 2)
    setup["vram_after_tts_load_mib"] = gpu.now()

    t = time.perf_counter()
    prompt = tts.create_voice_clone_prompt(ref_audio=str(ROOT / c["ref_audio"]), ref_text=cfg["ref_text"],
                                           x_vector_only_mode=False)
    setup["voice_prompt_s"] = round(time.perf_counter() - t, 2)
    print("SETUP", json.dumps(setup), flush=True)

    runs = []
    for i in range(1, args.runs + 1):
        r = {"run": i}
        t0 = time.perf_counter()

        segs, info = asr.transcribe(args.inp, language=a["language"], beam_size=a["beam_size"],
                                    vad_filter=a["vad_filter"])
        r["asr_text"] = "".join(s.text for s in segs).strip()   # generator: consuming it runs the decode
        r["asr_done_s"] = round(time.perf_counter() - t0, 3)
        r["input_audio_s"] = round(info.duration, 2)

        reply, first = llm_stream(cfg, r["asr_text"], t0)
        r["llm_first_token_s"] = round(first, 3) if first else None
        r["llm_done_s"] = round(time.perf_counter() - t0, 3)
        r["reply_text"] = reply

        wavs, sr = tts.generate_voice_clone(text=reply, language=c["language"], voice_clone_prompt=prompt)
        target = out_wav if i == args.runs else out_wav.with_name(f"{out_wav.stem}_run{i}.wav")
        sf.write(str(target), wavs[0], sr)
        r["tts_done_s"] = round(time.perf_counter() - t0, 3)
        r["total_s"] = r["tts_done_s"]
        r["stage_s"] = {"asr": r["asr_done_s"],
                        "llm": round(r["llm_done_s"] - r["asr_done_s"], 3),
                        "tts": round(r["tts_done_s"] - r["llm_done_s"], 3)}
        r["reply_audio_s"] = round(len(wavs[0]) / sr, 2)
        r["tts_rtf"] = round(r["stage_s"]["tts"] / r["reply_audio_s"], 3) if r["reply_audio_s"] else None
        r["output"] = str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target)
        runs.append(r)
        print("RUN", json.dumps(r, ensure_ascii=False), flush=True)

    gpu.stop_flag = True
    result = {"input": args.inp, "setup": setup, "runs": runs,
              "vram_peak_total_gpu_mib": gpu.peak,
              "torch_max_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20),
              "config": {k: cfg[k] for k in ("asr", "llm", "tts")}}
    (out_dir / "timings.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT", json.dumps({"vram_peak_total_gpu_mib": gpu.peak,
                                "torch_max_reserved_mib": result["torch_max_reserved_mib"]}), flush=True)


if __name__ == "__main__":
    sys.exit(main())
