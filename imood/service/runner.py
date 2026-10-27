"""Run one job: wav -> ASR -> LLM (streamed) -> TTS (resampled) -> LiveTalking, with BERT emotion in parallel.

Same flow and timings as the legacy voice service; the pieces now live in intake/transcriber, dialogue/, voice/tts_voice.
Emotion: the reply text goes to the BERT UDP service at the same time as to TTS; at the first audio chunk the result is
used if it is ready, otherwise the default (never waited for).
"""
import http.client, json, queue, threading, time, traceback, urllib.request

from imood.common import config as cfgmod
from imood.common.labels import code_to_vendor
from imood.dialogue.llm_client import llm_deltas
from imood.dialogue.segmenter import Segmenter
from imood.emotion.udp_client import EmotionClientSync
from imood.intake import transcriber
from imood.service.state import CFG, LT, M, ROOT, V2
from imood.voice import tts_voice
from step12_mvp import discover_session, lt_get    # not moved yet (scripts/wsl)


def _emotion_client():
    c = cfgmod.load().get("emotion", {})
    if not c.get("enabled", True):
        return None
    return EmotionClientSync(c.get("udp_host", "127.0.0.1"), int(c.get("udp_port", 8102)), int(c.get("timeout_ms", 300)))


def _switch_face(base, sessionid, vendor_emotion):
    """Interim: ask the LiveTalking adapter to change the face (the framed downstream link will do this itself)."""
    try:
        req = urllib.request.Request(f"{base}/cyber_gf/emotion?sessionid={sessionid}&emotion={vendor_emotion}", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


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
    job.transcript, duration = transcriber.transcribe(M["asr"], job.wav, a)
    job.mark("asr_done"); job.metrics["input_audio_s"] = round(duration, 2)

    # ---- producer (TTS) / consumer (sender) plumbing
    text_q, audio_q = queue.Queue(), queue.Queue()
    utt = job.id
    rs = soxr.ResampleStream(V2["resample"]["from_hz"], V2["resample"]["to_hz"], 1, dtype="float32")
    raw24, sent16 = [], []
    stat = {"tts_chunks": 0, "audio_q_max": 0, "tts_segments": 0}
    tts_kw = dict(language=c["language"], ref_audio=str(ROOT / c["ref_audio"]), ref_text=CFG["ref_text"],
                  xvec_only=tf["xvec_only"], append_silence=tf["append_silence"], chunk_size=tf["chunk_size"])
    emo = {"client": _emotion_client(), "decided": False, "started": False}

    def emit(b):
        if not emo["decided"]:                          # first audio chunk: take BERT's answer if ready, else the default
            emo["decided"] = True
            if emo["client"] is not None:
                code, ready = emo["client"].result()
                vendor = code_to_vendor(code)
                job.metrics["emotion"] = {"code": code, "vendor": vendor, "bert_ready": ready}
                threading.Thread(target=_switch_face, args=(base, job.sessionid, vendor), daemon=True).start()
        audio_q.put(b)
        stat["audio_q_max"] = max(stat["audio_q_max"], audio_q.qsize())

    def producer():
        try:
            tts_voice.stream_segments(M["tts"], tts_kw, text_q, rs, emit, stat, job.mark, raw24, sent16)
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

    def to_tts(s):
        if emo["client"] is not None and not emo["started"]:          # reply text goes to BERT and TTS at the same moment
            emo["started"] = True
            emo["client"].start(s)
        text_q.put(s)

    tp, ts_ = threading.Thread(target=producer, daemon=True), threading.Thread(target=sender, daemon=True)
    tp.start(); ts_.start()

    # ---- LLM (streamed) -> segments
    job.state = "thinking"
    seg = Segmenter(V2["llm_to_tts"])
    sentence_mode = job.mode == "sentence"
    for d in llm_deltas(CFG, job.transcript):
        job.mark("llm_first_token")
        job.reply += d
        if sentence_mode:
            for s in seg.feed(d):
                job.segments.append(s); job.mark("first_segment_ready"); to_tts(s); job.state = "synthesizing"
    job.mark("llm_done")
    rest = seg.flush() if sentence_mode else [job.reply]
    for s in rest:
        if s:
            job.segments.append(s); job.mark("first_segment_ready"); to_tts(s)
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
