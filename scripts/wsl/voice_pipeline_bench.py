"""Voice pipeline benchmark (no mic / WebRTC / LiveTalking):

    input.wav -> Faster-Whisper large-v3 -> llama.cpp Qwen3-14B (:8090) -> Qwen3-TTS 1.7B Base clone -> reply.wav

Model loading and the voice-clone prompt are built once and timed separately; the per-run
timings (ASR, LLM first token / done, TTS, end-to-end) exclude them.
Run through scripts/wsl/run_voice_pipeline_bench.sh (sets CUDA library paths for CTranslate2).
"""
import argparse, json, re, subprocess, sys, threading, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from imood.common.voice_settings import load_voice_config


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


def llm_stream(cfg, user_text, started):
    body = {"messages": [{"role": "system", "content": cfg["persona"]}, {"role": "user", "content": user_text}],
            "stream": True, "temperature": cfg["llm"]["temperature"], "top_p": cfg["llm"]["top_p"],
            "max_tokens": cfg["llm"]["max_tokens"]}
    request = urllib.request.Request(cfg["llm"]["url"], data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    first, text = None, ""
    with urllib.request.urlopen(request) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
            if delta.strip() and first is None:
                first = time.perf_counter() - started
            text += delta
    # Qwen3 may still emit an (empty) think block; never send it to TTS
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text, first


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", default=str(ROOT / "assets/test.wav"))
    parser.add_argument("--out", default=None, help="default: <output_dir>/reply.wav")
    parser.add_argument("--runs", type=int, default=2, help="run 1 = cold (first CUDA kernels), later runs = warm")
    args = parser.parse_args()

    cfg = load_voice_config()
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

    started = time.perf_counter()
    asr_config = cfg["asr"]
    asr_model = WhisperModel(asr_config["model"], device=asr_config["device"], compute_type=asr_config["compute_type"])
    setup["asr_load_s"] = round(time.perf_counter() - started, 2)
    setup["vram_after_asr_load_mib"] = gpu.now()

    started = time.perf_counter()
    tts_config = cfg["tts"]
    tts = Qwen3TTSModel.from_pretrained(tts_config["model"], device_map=tts_config["device"], dtype=getattr(torch, tts_config["dtype"]),
                                        attn_implementation=tts_config["attn_implementation"])
    setup["tts_load_s"] = round(time.perf_counter() - started, 2)
    setup["vram_after_tts_load_mib"] = gpu.now()

    started = time.perf_counter()
    prompt = tts.create_voice_clone_prompt(ref_audio=str(ROOT / tts_config["ref_audio"]), ref_text=cfg["ref_text"],
                                           x_vector_only_mode=False)
    setup["voice_prompt_s"] = round(time.perf_counter() - started, 2)
    print("SETUP", json.dumps(setup), flush=True)

    runs = []
    for i in range(1, args.runs + 1):
        run_result = {"run": i}
        run_start = time.perf_counter()

        segments, info = asr_model.transcribe(args.inp, language=asr_config["language"], beam_size=asr_config["beam_size"],
                                    vad_filter=asr_config["vad_filter"])
        run_result["asr_text"] = "".join(segment.text for segment in segments).strip()   # generator: consuming it runs the decode
        run_result["asr_done_s"] = round(time.perf_counter() - run_start, 3)
        run_result["input_audio_s"] = round(info.duration, 2)

        reply, first = llm_stream(cfg, run_result["asr_text"], run_start)
        run_result["llm_first_token_s"] = round(first, 3) if first else None
        run_result["llm_done_s"] = round(time.perf_counter() - run_start, 3)
        run_result["reply_text"] = reply

        wavs, sample_rate = tts.generate_voice_clone(text=reply, language=tts_config["language"], voice_clone_prompt=prompt)
        target = out_wav if i == args.runs else out_wav.with_name(f"{out_wav.stem}_run{i}.wav")
        sf.write(str(target), wavs[0], sample_rate)
        run_result["tts_done_s"] = round(time.perf_counter() - run_start, 3)
        run_result["total_s"] = run_result["tts_done_s"]
        run_result["stage_s"] = {"asr": run_result["asr_done_s"],
                        "llm": round(run_result["llm_done_s"] - run_result["asr_done_s"], 3),
                        "tts": round(run_result["tts_done_s"] - run_result["llm_done_s"], 3)}
        run_result["reply_audio_s"] = round(len(wavs[0]) / sample_rate, 2)
        run_result["tts_rtf"] = round(run_result["stage_s"]["tts"] / run_result["reply_audio_s"], 3) if run_result["reply_audio_s"] else None
        run_result["output"] = str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target)
        runs.append(run_result)
        print("RUN", json.dumps(run_result, ensure_ascii=False), flush=True)

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
