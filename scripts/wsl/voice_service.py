"""Step 12 v2 long-lived voice service (Ubuntu-24.04, .venv-tts-fast).

Models are loaded ONCE at startup (Faster-Whisper, FasterQwen3TTS + CUDA-graph capture + ref-prompt warm-up).
Per question:

  wav -> ASR -> llama.cpp (streamed) -> [sentence segmenter] -> FasterQwen3TTS.generate_voice_clone_streaming(chunk_size=8)
      -> soxr ResampleStream 24k->16k (one stateful resampler per utterance, so no chunk-boundary artifacts)
      -> in-memory queue -> ONE chunked HTTP POST per utterance to LiveTalking /cyber_gf/audio_stream
         (adapter frames it into 20 ms put_audio_frame() calls: one 'start', one 'end')

The TTS producer never waits for playback; a separate sender thread drains the queue concurrently.

HTTP API (CORS enabled so the imood page on :8010 can call it):
  POST /api/ask?sessionid=<optional>&mode=<sentence|full>   body = question WAV bytes  -> {"job": id}
  GET  /api/job/<id>                                                                    -> state/texts/metrics
  GET  /api/health
"""
import argparse, datetime, http.client, io, json, os, queue, re, sys, threading, time, traceback, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from voice_pipeline_test import load_cfg          # Step 9: voice.json + persona + ref_text
from step12_mvp import discover_session, lt_get    # Step 12 MVP: automatic LiveTalking session discovery

ROOT = HERE.parents[1]
CFG = load_cfg()
V2 = json.loads((ROOT / "config/step12_v2.json").read_text(encoding="utf-8"))
LT = V2["livetalking"]
JOBS, JOB_LOCK, JOB_Q = {}, threading.Lock(), queue.Queue()
M = {}  # loaded models


# ---------------------------------------------------------------- LLM streaming + segmentation
def llm_deltas(user_text):
    """Same request as Step 9 llm_stream (persona system prompt, temperature, top_p, max_tokens), yielded incrementally."""
    body = {"messages": [{"role": "system", "content": CFG["persona"]}, {"role": "user", "content": user_text}],
            "stream": True, "temperature": CFG["llm"]["temperature"], "top_p": CFG["llm"]["top_p"],
            "max_tokens": CFG["llm"]["max_tokens"]}
    req = urllib.request.Request(CFG["llm"]["url"], data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    in_think = False
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            d = json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
            # never forward a (possibly empty) Qwen3 think block
            while d:
                if in_think:
                    i = d.find("</think>")
                    if i < 0: d = ""; break
                    d = d[i + 8:]; in_think = False
                else:
                    i = d.find("<think>")
                    if i < 0: yield d; break
                    if i: yield d[:i]
                    d = d[i + 7:]; in_think = True


class Segmenter:
    """Emit whole sentences (ending in 。！？…); merge tiny ones; comma fallback for very long runs."""

    def __init__(self, s):
        self.end, self.comma = set(s["sentence_end_chars"]), set(s["comma_chars"])
        self.min, self.max, self.buf = s["min_segment_chars"], s["max_segment_chars"], ""

    @staticmethod
    def speakable(t):
        return len(re.sub(r"[\W_]+", "", t))

    def feed(self, text):
        self.buf += text
        out, start = [], 0
        i = 0
        while i < len(self.buf):
            if self.buf[i] in self.end:
                j = i + 1
                while j < len(self.buf) and (self.buf[j] in self.end or self.buf[j] in "」』”’）)\"'"):
                    j += 1
                if self.speakable(self.buf[start:j]) >= self.min:
                    out.append(self.buf[start:j]); start = j
                i = j
                continue
            i += 1
        rest = self.buf[start:]
        if self.speakable(rest) >= self.max:              # safety fallback: cut at the last comma
            k = max((rest.rfind(c) for c in self.comma), default=-1)
            if k > 0 and self.speakable(rest[:k + 1]) >= self.min:
                out.append(rest[:k + 1]); start += k + 1
        self.buf = self.buf[start:]
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        return [rest] if rest else []


# ---------------------------------------------------------------- job
class Job:
    def __init__(self, wav, sessionid, mode):
        self.id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
        self.wav, self.sessionid, self.mode = wav, sessionid, mode
        self.state, self.error = "queued", None
        self.transcript, self.reply, self.segments = "", "", []
        self.wall0 = time.time(); self.t0 = time.perf_counter()
        self.ts, self.metrics, self.log = {"request_received": 0.0}, {}, []

    def mark(self, key, wall=None):
        v = (wall - self.wall0) if wall else (time.perf_counter() - self.t0)
        self.ts.setdefault(key, round(v, 3))

    def info(self):
        return {"job": self.id, "state": self.state, "error": self.error, "mode": self.mode, "sessionid": self.sessionid,
                "transcript": self.transcript, "reply": self.reply, "segments": self.segments,
                "timestamps_s": self.ts, "metrics": self.metrics, "log": self.log[-30:]}


def run_job(job):
    import numpy as np, soundfile as sf, soxr
    a, c, tf = CFG["asr"], CFG["tts"], V2["tts_fast"]
    out = ROOT / V2["output_dir"] / job.id
    out.mkdir(parents=True, exist_ok=True)
    (out / "question.wav").write_bytes(job.wav)
    base = LT["url"]

    # session: from the page, else automatic discovery (MVP logic)
    if not job.sessionid:
        try:
            job.sessionid, _ = discover_session(base, LT["avatar_id"])
        except SystemExit as e:
            raise RuntimeError(str(e).strip())
    else:
        active = [s["sessionid"] for s in lt_get(base, "/api/admin/sessions")["data"]["sessions"]]
        if job.sessionid not in active:
            raise RuntimeError(f"LiveTalking session {job.sessionid} is not active - reconnect the avatar page")

    # ---- ASR
    job.state = "asr"
    segs, info = M["asr"].transcribe(io.BytesIO(job.wav), language=a["language"], beam_size=a["beam_size"], vad_filter=a["vad_filter"])
    job.transcript = "".join(s.text for s in segs).strip()
    job.mark("asr_done"); job.metrics["input_audio_s"] = round(info.duration, 2)

    # ---- producer (TTS) / consumer (sender) plumbing
    text_q, audio_q = queue.Queue(), queue.Queue()
    utt = job.id
    rs = soxr.ResampleStream(V2["resample"]["from_hz"], V2["resample"]["to_hz"], 1, dtype="float32")
    raw24, sent16 = [], []
    stat = {"tts_chunks": 0, "audio_q_max": 0, "tts_segments": 0}
    tts_kw = dict(language=c["language"], ref_audio=str(ROOT / c["ref_audio"]), ref_text=CFG["ref_text"],
                  xvec_only=tf["xvec_only"], append_silence=tf["append_silence"], chunk_size=tf["chunk_size"])

    def producer():
        try:
            while True:
                seg = text_q.get()
                if seg is None:
                    break
                if not Segmenter.speakable(seg):
                    continue
                stat["tts_segments"] += 1
                for chunk, sr, _t in M["tts"].generate_voice_clone_streaming(text=seg.strip(), **tts_kw):
                    x = np.asarray(chunk, dtype=np.float32).flatten()
                    raw24.append(x); stat["tts_chunks"] += 1
                    job.mark("tts_first_chunk")
                    y = rs.resample_chunk(x)
                    if len(y):
                        sent16.append(y); audio_q.put(y.astype("<f4").tobytes())
                        stat["audio_q_max"] = max(stat["audio_q_max"], audio_q.qsize())
            y = rs.resample_chunk(np.zeros(0, np.float32), last=True)
            if len(y):
                sent16.append(y); audio_q.put(y.astype("<f4").tobytes())
            job.mark("tts_done")
        except Exception as e:
            job.error = f"TTS: {e}"; job.log.append(traceback.format_exc())
        finally:
            audio_q.put(None)

    sender_result = {}

    def sender():
        def body():
            while True:
                b = audio_q.get()
                if b is None:
                    return
                job.mark("first_audio_submitted")
                yield b
        try:
            conn = http.client.HTTPConnection(base.split("//")[1], timeout=300)
            conn.request("POST", f"/cyber_gf/audio_stream?sessionid={job.sessionid}&utt={utt}", body=body(),
                         headers={"Content-Type": "application/octet-stream"}, encode_chunked=True)
            sender_result.update(json.loads(conn.getresponse().read()))
            job.mark("audio_stream_closed")
        except Exception as e:
            job.error = f"send: {e}"; job.log.append(traceback.format_exc())

    tp, ts_ = threading.Thread(target=producer, daemon=True), threading.Thread(target=sender, daemon=True)
    tp.start(); ts_.start()

    # ---- LLM (streamed) -> segments
    job.state = "thinking"
    seg = Segmenter(V2["llm_to_tts"])
    sentence_mode = job.mode == "sentence"
    for d in llm_deltas(job.transcript):
        job.mark("llm_first_token")
        job.reply += d
        if sentence_mode:
            for s in seg.feed(d):
                job.segments.append(s); job.mark("first_segment_ready"); text_q.put(s); job.state = "synthesizing"
    job.mark("llm_done")
    rest = seg.flush() if sentence_mode else [job.reply]
    for s in rest:
        if s:
            job.segments.append(s); job.mark("first_segment_ready"); text_q.put(s)
    text_q.put(None)
    job.state = "synthesizing"
    assert "".join(job.segments) == job.reply, "segmentation must preserve the exact reply text"

    # ---- wait for playback start / end (reported by the LiveTalking adapter)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            u = lt_get(base, f"/cyber_gf/utt/{utt}")
        except Exception:
            u = {}
        d = u.get("data") or {}
        if d.get("first_frame_put"):
            job.mark("first_frame_put_livetalking", d["first_frame_put"])
        if d.get("playback_start"):
            job.mark("avatar_speaking_start", d["playback_start"]); job.state = "speaking"
        if d.get("playback_end"):
            job.mark("avatar_speaking_end", d["playback_end"]); job.metrics["livetalking"] = d; break
        if job.error and not tp.is_alive() and not ts_.is_alive():
            break
        time.sleep(0.05)
    tp.join(5); ts_.join(5)

    audio16 = np.concatenate(sent16) if sent16 else np.zeros(0, np.float32)
    audio24 = np.concatenate(raw24) if raw24 else np.zeros(0, np.float32)
    sf.write(str(out / "reply_24k_tts.wav"), audio24, 24000)
    sf.write(str(out / "reply_16k_sent.wav"), audio16, 16000)
    lt_d = job.metrics.get("livetalking") or sender_result.get("data") or {}
    job.metrics.update({
        "tts_chunks": stat["tts_chunks"], "tts_segments": stat["tts_segments"], "service_audio_queue_max": stat["audio_q_max"],
        "audio_s_tts_24k": round(len(audio24) / 24000, 3), "audio_s_sent_16k": round(len(audio16) / 16000, 3),
        "lt_frames": lt_d.get("frames"), "lt_max_queue_frames": lt_d.get("max_queue_frames"),
        "lt_underruns": lt_d.get("underruns"), "lt_underrun_frames": lt_d.get("underrun_frames"),
        "lt_overflows": lt_d.get("overflows"), "lt_fps": lt_d.get("lt_fps"),
        "tts_rtf": round((job.ts.get("tts_done", 0) - job.ts.get("first_segment_ready", 0)) / max(len(audio24) / 24000, 1e-6), 3),
    })
    if "avatar_speaking_start" in job.ts and "avatar_speaking_end" in job.ts:
        job.metrics["avatar_spoke_s"] = round(job.ts["avatar_speaking_end"] - job.ts["avatar_speaking_start"], 3)
    (out / "transcript.txt").write_text(job.transcript + "\n", encoding="utf-8")
    (out / "reply.txt").write_text(job.reply + "\n", encoding="utf-8")
    job.state = "error" if job.error else "done"
    (out / "timings.json").write_text(json.dumps(job.info(), ensure_ascii=False, indent=2), encoding="utf-8")
    job.metrics["output_dir"] = str(out.relative_to(ROOT))


def worker():
    while True:
        job = JOB_Q.get()
        try:
            run_job(job)
        except Exception as e:
            job.error = str(e); job.state = "error"; job.log.append(traceback.format_exc())
        print("JOB", json.dumps(job.info(), ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/health":
            return self._send(200, {"ok": True, "models": list(M), "setup_s": M.get("_setup")})
        if p.startswith("/api/job/"):
            j = JOBS.get(p.rsplit("/", 1)[1])
            return self._send(200 if j else 404, j.info() if j else {"error": "unknown job"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path != "/api/ask":
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        wav = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if len(wav) < 100:
            return self._send(400, {"error": "empty audio"})
        mode = (q.get("mode") or [V2["llm_to_tts"]["mode"]])[0]
        job = Job(wav, (q.get("sessionid") or [""])[0], mode if mode in ("sentence", "full") else "sentence")
        JOBS[job.id] = job; JOB_Q.put(job)
        self._send(200, {"job": job.id})

    def log_message(self, *a):
        pass


def load_models():
    import torch
    from faster_whisper import WhisperModel
    from faster_qwen3_tts import FasterQwen3TTS
    a, c, tf = CFG["asr"], CFG["tts"], V2["tts_fast"]
    s = {}
    t = time.perf_counter(); M["asr"] = WhisperModel(a["model"], device=a["device"], compute_type=a["compute_type"])
    s["asr_load_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    M["tts"] = FasterQwen3TTS.from_pretrained(c["model"], device="cuda", dtype=getattr(torch, c["dtype"]),
                                              attn_implementation=c["attn_implementation"], max_seq_len=tf["max_seq_len"])
    M["tts"].warmup(prefill_len=100)
    s["tts_load_and_graph_capture_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()   # cache the ref prompt + warm the streaming decode path (outputs discarded)
    kw = dict(language=c["language"], ref_audio=str(ROOT / c["ref_audio"]), ref_text=CFG["ref_text"],
              xvec_only=tf["xvec_only"], append_silence=tf["append_silence"])
    M["tts"].generate_voice_clone(text="嗯。", **kw)
    for _ in M["tts"].generate_voice_clone_streaming(text="你好呀，我在呢。", chunk_size=tf["chunk_size"], **kw):
        pass
    s["tts_warmup_s"] = round(time.perf_counter() - t, 2)
    M["_setup"] = s
    print("SETUP", json.dumps(s), flush=True)


def main():
    if os.environ.get("WSL_DISTRO_NAME") != V2["wsl_distro"]:
        sys.exit(f"Must run in WSL {V2['wsl_distro']} (current: {os.environ.get('WSL_DISTRO_NAME')!r})")
    load_models()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((V2["service"]["host"], V2["service"]["port"]), H)
    print(f"READY voice service on :{V2['service']['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
