"""Step 12 MVP (no streaming):

    test.wav -> Faster-Whisper -> llama.cpp Qwen3 (:8090) -> faster-qwen3-tts voice clone -> complete reply.wav
             -> POST /humanaudio to the active LiveTalking WebRTC session -> wav2lip256_myavatar speaks

ASR settings, LLM call (llm_stream) and persona/ref loading are reused from the Step 9 script.
Run via scripts/wsl/run_batch_reply.sh (Ubuntu-24.04, .venv-tts-fast).
"""
import argparse, datetime, json, os, sys, time, urllib.request, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from voice_pipeline_bench import llm_stream
from imood.common.voice_settings import load_voice_config
from imood.downstream.livetalking_api import discover_session, lt_post_json


# ---------------------------------------------------------------- LiveTalking helpers
def post_humanaudio(base, sessionid, wav_path):
    """multipart/form-data: field 'sessionid' + file field 'file' (exactly what server/routes.py:humanaudio reads)."""
    boundary = uuid.uuid4().hex
    data = Path(wav_path).read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"sessionid\"\r\n\r\n{sessionid}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{Path(wav_path).name}\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(base + "/humanaudio", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", default=str(ROOT / "assets/test.wav"))
    parser.add_argument("--session", default=os.environ.get("LT_SESSION_ID"), help="override auto-discovery")
    args = parser.parse_args()

    cfg = load_voice_config()                                          # voice.json + persona + ref_text
    batch_config = json.loads((ROOT / "config/batch_reply.json").read_text(encoding="utf-8"))
    if os.environ.get("WSL_DISTRO_NAME") != batch_config["wsl_distro"]:
        sys.exit(f"Must run in WSL {batch_config['wsl_distro']} (current: {os.environ.get('WSL_DISTRO_NAME')!r})")
    livetalking_config, tts_fast_config, asr_config, tts_config = batch_config["livetalking"], batch_config["tts_fast"], cfg["asr"], cfg["tts"]
    base = livetalking_config["url"]
    run_dir = ROOT / batch_config["output_dir"] / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    # ---- preflight (fail fast, before loading models)
    urllib.request.urlopen(cfg["llm"]["url"].split("/v1/")[0] + "/health", timeout=3)
    sessionid, _ = discover_session(base, livetalking_config["avatar_id"], args.session)
    print(f"[ok] llama.cpp reachable; LiveTalking session {sessionid} ({livetalking_config['avatar_id']})", flush=True)

    # ---- one-time setup (not part of end-to-end timing)
    setup = {}
    import torch, numpy as np, soundfile as sf
    from faster_whisper import WhisperModel
    from faster_qwen3_tts import FasterQwen3TTS
    started = time.perf_counter()
    asr_model = WhisperModel(asr_config["model"], device=asr_config["device"], compute_type=asr_config["compute_type"])
    setup["asr_load_s"] = round(time.perf_counter() - started, 2)
    started = time.perf_counter()
    tts = FasterQwen3TTS.from_pretrained(tts_config["model"], device="cuda", dtype=getattr(torch, tts_config["dtype"]),
                                         attn_implementation=tts_config["attn_implementation"], max_seq_len=tts_fast_config["max_seq_len"])
    tts.warmup(prefill_len=100)                                         # CUDA-graph capture
    setup["tts_load_and_graph_capture_s"] = round(time.perf_counter() - started, 2)
    ref_audio, ref_text = str(ROOT / tts_config["ref_audio"]), cfg["ref_text"]
    tts_kw = dict(language=tts_config["language"], ref_audio=ref_audio, ref_text=ref_text,
                  xvec_only=tts_fast_config["xvec_only"], append_silence=tts_fast_config["append_silence"])
    started = time.perf_counter()
    tts.generate_voice_clone(text="嗯。", **tts_kw)                      # caches the ref prompt; output discarded
    setup["tts_warmup_s"] = round(time.perf_counter() - started, 2)
    print("SETUP", json.dumps(setup), flush=True)

    # ---- end-to-end run
    wall_start = time.time(); perf_start = time.perf_counter()
    timestamps = {"input_start": 0.0}
    elapsed = lambda: round(time.perf_counter() - perf_start, 3)

    segments, info = asr_model.transcribe(args.inp, language=asr_config["language"], beam_size=asr_config["beam_size"], vad_filter=asr_config["vad_filter"])
    transcript = "".join(segment.text for segment in segments).strip()
    timestamps["asr_complete"] = elapsed()

    reply, first = llm_stream(cfg, transcript, perf_start)
    timestamps["llm_first_token"] = round(first, 3) if first else None
    timestamps["llm_complete"] = elapsed()

    wavs, sample_rate = tts.generate_voice_clone(text=reply, **tts_kw)
    wav = np.asarray(wavs[0]).flatten()
    reply_wav = run_dir / "reply.wav"
    sf.write(str(reply_wav), wav, sample_rate)
    timestamps["tts_complete"] = elapsed()

    humanaudio_response = post_humanaudio(base, sessionid, reply_wav)
    timestamps["humanaudio_post_complete"] = elapsed()
    if humanaudio_response.get("code") != 0:
        sys.exit(f"/humanaudio failed: {humanaudio_response}")

    # LiveTalking speaking start/end, via POST /is_speaking polling
    deadline = time.perf_counter() + livetalking_config["speaking_start_timeout_s"]
    while time.perf_counter() < deadline:
        if lt_post_json(base, "/is_speaking", {"sessionid": sessionid}).get("data"):
            timestamps["livetalking_speaking_start"] = elapsed(); break
        time.sleep(livetalking_config["speaking_poll_s"])
    audio_s = len(wav) / sample_rate
    if "livetalking_speaking_start" in timestamps:
        deadline = time.perf_counter() + audio_s + 10
        while time.perf_counter() < deadline:
            if not lt_post_json(base, "/is_speaking", {"sessionid": sessionid}).get("data"):
                timestamps["livetalking_speaking_end"] = elapsed(); break
            time.sleep(livetalking_config["speaking_poll_s"])

    result = {
        "input": args.inp, "input_audio_s": round(info.duration, 2), "sessionid": sessionid,
        "avatar_id": livetalking_config["avatar_id"], "started_at": datetime.datetime.fromtimestamp(wall_start).isoformat(timespec="milliseconds"),
        "transcript": transcript, "reply_text": reply,
        "reply_wav": str(reply_wav.relative_to(ROOT)), "reply_audio_s": round(audio_s, 2), "sample_rate": sample_rate,
        "timestamps_s": timestamps,
        "stages_s": {"asr": timestamps["asr_complete"],
                     "llm_to_first_token": round((timestamps["llm_first_token"] or 0) - timestamps["asr_complete"], 3),
                     "llm": round(timestamps["llm_complete"] - timestamps["asr_complete"], 3),
                     "tts": round(timestamps["tts_complete"] - timestamps["llm_complete"], 3),
                     "humanaudio_post": round(timestamps["humanaudio_post_complete"] - timestamps["tts_complete"], 3),
                     "post_to_speaking_start": round(timestamps["livetalking_speaking_start"] - timestamps["humanaudio_post_complete"], 3)
                     if "livetalking_speaking_start" in timestamps else None},
        "tts_rtf": round((timestamps["tts_complete"] - timestamps["llm_complete"]) / audio_s, 3),
        "setup_s": setup,
        "config": {"asr": asr_config, "llm_url": cfg["llm"]["url"], "tts_model": tts_config["model"], "tts_fast": tts_fast_config, "livetalking": livetalking_config},
    }
    (run_dir / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
    (run_dir / "reply.txt").write_text(reply + "\n", encoding="utf-8")
    (run_dir / "timings.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TRANSCRIPT", transcript)
    print("REPLY", reply)
    print("TIMESTAMPS", json.dumps(timestamps))
    print("OUTPUT", run_dir.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    sys.exit(main())
