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
from imood.downstream.livetalking_api import discover_session, lt_get, lt_post_json


# ---------------------------------------------------------------- LiveTalking helpers
def post_humanaudio(base, sessionid, wav_path):
    """multipart/form-data: field 'sessionid' + file field 'file' (exactly what server/routes.py:humanaudio reads)."""
    boundary = uuid.uuid4().hex
    data = Path(wav_path).read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"sessionid\"\r\n\r\n{sessionid}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{Path(wav_path).name}\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base + "/humanaudio", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(ROOT / "assets/test.wav"))
    ap.add_argument("--session", default=os.environ.get("LT_SESSION_ID"), help="override auto-discovery")
    args = ap.parse_args()

    cfg = load_voice_config()                                          # voice.json + persona + ref_text
    s12 = json.loads((ROOT / "config/batch_reply.json").read_text(encoding="utf-8"))
    if os.environ.get("WSL_DISTRO_NAME") != s12["wsl_distro"]:
        sys.exit(f"Must run in WSL {s12['wsl_distro']} (current: {os.environ.get('WSL_DISTRO_NAME')!r})")
    lt, tf, a, c = s12["livetalking"], s12["tts_fast"], cfg["asr"], cfg["tts"]
    base = lt["url"]
    run_dir = ROOT / s12["output_dir"] / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    # ---- preflight (fail fast, before loading models)
    urllib.request.urlopen(cfg["llm"]["url"].split("/v1/")[0] + "/health", timeout=3)
    sessionid, _ = discover_session(base, lt["avatar_id"], args.session)
    print(f"[ok] llama.cpp reachable; LiveTalking session {sessionid} ({lt['avatar_id']})", flush=True)

    # ---- one-time setup (not part of end-to-end timing)
    setup = {}
    import torch, numpy as np, soundfile as sf
    from faster_whisper import WhisperModel
    from faster_qwen3_tts import FasterQwen3TTS
    t = time.perf_counter()
    asr = WhisperModel(a["model"], device=a["device"], compute_type=a["compute_type"])
    setup["asr_load_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    tts = FasterQwen3TTS.from_pretrained(c["model"], device="cuda", dtype=getattr(torch, c["dtype"]),
                                         attn_implementation=c["attn_implementation"], max_seq_len=tf["max_seq_len"])
    tts.warmup(prefill_len=100)                                         # CUDA-graph capture
    setup["tts_load_and_graph_capture_s"] = round(time.perf_counter() - t, 2)
    ref_audio, ref_text = str(ROOT / c["ref_audio"]), cfg["ref_text"]
    tts_kw = dict(language=c["language"], ref_audio=ref_audio, ref_text=ref_text,
                  xvec_only=tf["xvec_only"], append_silence=tf["append_silence"])
    t = time.perf_counter()
    tts.generate_voice_clone(text="嗯。", **tts_kw)                      # caches the ref prompt; output discarded
    setup["tts_warmup_s"] = round(time.perf_counter() - t, 2)
    print("SETUP", json.dumps(setup), flush=True)

    # ---- end-to-end run
    wall0 = time.time(); t0 = time.perf_counter()
    ts = {"input_start": 0.0}
    rel = lambda: round(time.perf_counter() - t0, 3)

    segs, info = asr.transcribe(args.inp, language=a["language"], beam_size=a["beam_size"], vad_filter=a["vad_filter"])
    transcript = "".join(s.text for s in segs).strip()
    ts["asr_complete"] = rel()

    reply, first = llm_stream(cfg, transcript, t0)
    ts["llm_first_token"] = round(first, 3) if first else None
    ts["llm_complete"] = rel()

    wavs, sr = tts.generate_voice_clone(text=reply, **tts_kw)
    wav = np.asarray(wavs[0]).flatten()
    reply_wav = run_dir / "reply.wav"
    sf.write(str(reply_wav), wav, sr)
    ts["tts_complete"] = rel()

    resp = post_humanaudio(base, sessionid, reply_wav)
    ts["humanaudio_post_complete"] = rel()
    if resp.get("code") != 0:
        sys.exit(f"/humanaudio failed: {resp}")

    # LiveTalking speaking start/end, via POST /is_speaking polling
    deadline = time.perf_counter() + lt["speaking_start_timeout_s"]
    while time.perf_counter() < deadline:
        if lt_post_json(base, "/is_speaking", {"sessionid": sessionid}).get("data"):
            ts["livetalking_speaking_start"] = rel(); break
        time.sleep(lt["speaking_poll_s"])
    audio_s = len(wav) / sr
    if "livetalking_speaking_start" in ts:
        deadline = time.perf_counter() + audio_s + 10
        while time.perf_counter() < deadline:
            if not lt_post_json(base, "/is_speaking", {"sessionid": sessionid}).get("data"):
                ts["livetalking_speaking_end"] = rel(); break
            time.sleep(lt["speaking_poll_s"])

    result = {
        "input": args.inp, "input_audio_s": round(info.duration, 2), "sessionid": sessionid,
        "avatar_id": lt["avatar_id"], "started_at": datetime.datetime.fromtimestamp(wall0).isoformat(timespec="milliseconds"),
        "transcript": transcript, "reply_text": reply,
        "reply_wav": str(reply_wav.relative_to(ROOT)), "reply_audio_s": round(audio_s, 2), "sample_rate": sr,
        "timestamps_s": ts,
        "stages_s": {"asr": ts["asr_complete"],
                     "llm_to_first_token": round((ts["llm_first_token"] or 0) - ts["asr_complete"], 3),
                     "llm": round(ts["llm_complete"] - ts["asr_complete"], 3),
                     "tts": round(ts["tts_complete"] - ts["llm_complete"], 3),
                     "humanaudio_post": round(ts["humanaudio_post_complete"] - ts["tts_complete"], 3),
                     "post_to_speaking_start": round(ts["livetalking_speaking_start"] - ts["humanaudio_post_complete"], 3)
                     if "livetalking_speaking_start" in ts else None},
        "tts_rtf": round((ts["tts_complete"] - ts["llm_complete"]) / audio_s, 3),
        "setup_s": setup,
        "config": {"asr": a, "llm_url": cfg["llm"]["url"], "tts_model": c["model"], "tts_fast": tf, "livetalking": lt},
    }
    (run_dir / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
    (run_dir / "reply.txt").write_text(reply + "\n", encoding="utf-8")
    (run_dir / "timings.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TRANSCRIPT", transcript)
    print("REPLY", reply)
    print("TIMESTAMPS", json.dumps(ts))
    print("OUTPUT", run_dir.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    sys.exit(main())
