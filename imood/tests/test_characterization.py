"""Behaviour-lock tests for the full-mode service, dialogue, voice, launcher and the interfaces of the emotion module.

No GPU, models or running services are used (fakes for ASR, LLM, TTS, LiveTalking and the framed link).
Run in the voice venv from the repo root:  python -m imood.tests.test_characterization
"""
import asyncio, http.client, json, queue, re, shutil, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def serve_http(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ---------------------------------------------------------------- dialogue

SEG_CFG = {"sentence_end_chars": "。！？!?；;…\n", "min_segment_chars": 8, "max_segment_chars": 60, "comma_chars": "，,、"}


def test_segmenter():
    from imood.dialogue.segmenter import Segmenter
    assert Segmenter.speakable("你好，世界！ab_1") == 7
    assert Segmenter.speakable("。。！") == 0

    s = Segmenter(SEG_CFG)
    assert s.feed("你好。") == []                                    # too short: held back
    assert s.feed("今天天氣真的很好呀。") == ["你好。今天天氣真的很好呀。"]   # merged with the held piece
    assert s.buffer == "" and s.flush() == []

    s = Segmenter(SEG_CFG)
    assert s.feed("今天天氣真的很好啊！！然後") == ["今天天氣真的很好啊！！"]
    assert s.buffer == "然後" and s.flush() == ["然後"] and s.buffer == ""

    s = Segmenter(SEG_CFG)                                             # closing quote after the end mark stays with the sentence
    assert s.feed("他說：「今天天氣真的很好。」然後") == ["他說：「今天天氣真的很好。」"]

    s = Segmenter(SEG_CFG)                                             # no end mark: comma fallback only at >= max chars
    assert s.feed("甲" * 30 + "，" + "乙" * 29) == []
    out = s.feed("乙")
    assert out == ["甲" * 30 + "，"] and s.buffer == "乙" * 30

    s = Segmenter(SEG_CFG)                                             # char-by-char feeding gives the same result as one feed
    pieces = []
    for ch in "今天天氣真的很好呀。我們一起去公園散步吧！":
        pieces += s.feed(ch)
    assert pieces == ["今天天氣真的很好呀。", "我們一起去公園散步吧！"], pieces


class _LlmHandler(BaseHTTPRequestHandler):
    deltas, seen = [], {}

    def do_POST(self):
        _LlmHandler.seen = {"path": self.path, "ctype": self.headers.get("Content-Type"),
                            "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for d in _LlmHandler.deltas:
            self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": d}]}, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


def test_llm_deltas():
    from imood.dialogue.llm_client import llm_deltas
    srv, port = serve_http(_LlmHandler)
    cfg = {"persona": "P", "llm": {"url": f"http://127.0.0.1:{port}/v1/chat/completions", "temperature": 0.5,
                                   "top_p": 0.9, "max_tokens": 50}}

    def run(deltas):
        _LlmHandler.deltas = deltas
        return list(llm_deltas(cfg, "問題"))

    assert run([{"role": "assistant"}, {"content": "你好"}, {"content": None}, {"content": "世界"}]) == ["你好", "世界"]
    assert _LlmHandler.seen["path"] == "/v1/chat/completions" and _LlmHandler.seen["ctype"] == "application/json"
    assert _LlmHandler.seen["body"] == {
        "messages": [{"role": "system", "content": "P"}, {"role": "user", "content": "問題"}],
        "stream": True, "temperature": 0.5, "top_p": 0.9, "max_tokens": 50}
    assert run([{"content": "<think>\n\n</think>你好"}, {"content": "世界"}]) == ["你好", "世界"]
    assert run([{"content": "前<think>內部</think>後"}]) == ["前", "後"]
    assert run([{"content": "<think>a"}, {"content": "b</think>c"}]) == ["c"]      # think block spanning deltas
    assert run([{"content": "<think>only thinking"}]) == []
    srv.shutdown()


# ---------------------------------------------------------------- voice

class _FakeTts:
    def __init__(self):
        self.calls = []

    def generate_voice_clone_streaming(self, text, **kw):
        self.calls.append((text, kw))
        for k in range(2):
            yield np.full(2400, 0.1 * (k + 1), dtype=np.float32), 24000, 0.0


def test_stream_segments():
    import soxr
    from imood.voice import tts_voice
    tts, q = _FakeTts(), queue.Queue()
    for item in ["  你好呀  ", "。。", "再見", None, "ignored after None"]:
        q.put(item)
    rs = soxr.ResampleStream(24000, 16000, 1, dtype="float32")
    emitted, marks, raw24, sent16 = [], [], [], []
    stat = {"tts_chunks": 0, "audio_q_max": 0, "tts_segments": 0}
    tts_voice.stream_segments(tts, {"language": "Chinese", "chunk_size": 8}, q, rs, emitted.append, stat,
                              marks.append, raw24, sent16)
    assert [c[0] for c in tts.calls] == ["你好呀", "再見"]                # stripped; punctuation-only segment skipped
    assert tts.calls[0][1] == {"language": "Chinese", "chunk_size": 8}
    assert stat["tts_segments"] == 2 and stat["tts_chunks"] == 4 and marks == ["tts_first_chunk"] * 4
    assert len(raw24) == 4 and all(len(x) == 2400 for x in raw24)
    assert all(isinstance(b, bytes) and len(b) % 4 == 0 for b in emitted)
    assert sum(len(b) for b in emitted) // 4 == sum(len(y) for y in sent16)
    assert abs(sum(len(y) for y in sent16) - 4 * 1600) <= 64         # 24k -> 16k, flushed at the end
    assert q.get_nowait() == "ignored after None"


# ---------------------------------------------------------------- service: job, http api, run_job

def test_job():
    from imood.service.job import Job
    j = Job(b"wav", "sess", "full")
    assert re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{4}", j.id)
    assert (j.wav, j.sessionid, j.mode, j.state, j.error) == (b"wav", "sess", "full", "queued", None)
    assert (j.transcript, j.reply, j.segments, j.metrics, j.log) == ("", "", [], {}, [])
    assert j.timestamps == {"request_received": 0.0}
    j.mark("a", wall=j.wall_start + 1.23456)
    j.mark("a", wall=j.wall_start + 9)                                     # first mark wins
    assert j.timestamps["a"] == 1.235
    j.mark("b")
    assert 0 <= j.timestamps["b"] < 1
    j.log.extend(str(i) for i in range(40))
    info = j.info()
    assert list(info) == ["job", "state", "error", "mode", "sessionid", "transcript", "reply", "segments",
                          "timestamps_s", "metrics", "log"]
    assert info["log"] == [str(i) for i in range(10, 40)] and info["timestamps_s"] is j.timestamps
    from imood.service.job import normalize_mode
    assert [normalize_mode(m) for m in ("sentence", "full", "bogus", "", None, "FULL")] == ["sentence", "full", "sentence", "sentence", "sentence", "sentence"]


def _http(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body)
    r = c.getresponse()
    raw = r.read()
    return r.status, dict(r.getheaders()), (json.loads(raw) if raw else None)


def test_http_api():
    from imood.service import http_api
    from imood.service.state import JOB_QUEUE, JOBS, MODELS, SERVICE_CONFIG
    srv, port = serve_http(http_api.ApiHandler)
    try:
        st, hd, js = _http(port, "GET", "/api/health")
        assert st == 200 and js == {"ok": True, "models": list(MODELS), "setup_s": MODELS.get("_setup")}
        assert hd["Content-Type"] == "application/json; charset=utf-8" and hd["Access-Control-Allow-Origin"] == "*"
        assert _http(port, "GET", "/api/job/nope")[::2] == (404, {"error": "unknown job"})
        assert _http(port, "GET", "/nothing")[::2] == (404, {"error": "not found"})
        assert _http(port, "POST", "/api/other", b"x" * 200)[::2] == (404, {"error": "not found"})
        assert _http(port, "POST", "/api/ask", b"x" * 99)[::2] == (400, {"error": "empty audio"})
        st, hd, _ = _http(port, "OPTIONS", "/api/ask")
        assert st == 204 and hd["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS" and hd["Access-Control-Allow-Headers"] == "*"

        st, _, js = _http(port, "POST", "/api/ask?mode=bogus&sessionid=abc", b"x" * 100)
        job = JOBS[js["job"]]
        assert st == 200 and (job.mode, job.sessionid, job.wav) == ("sentence", "abc", b"x" * 100)
        assert JOB_QUEUE.get_nowait() is job
        st, _, js = _http(port, "POST", "/api/ask?mode=sentence", b"x" * 100)
        assert JOBS[js["job"]].mode == "sentence" and JOBS[js["job"]].sessionid == ""
        JOB_QUEUE.get_nowait()
        st, _, js = _http(port, "POST", "/api/ask", b"x" * 100)
        assert JOBS[js["job"]].mode == SERVICE_CONFIG["llm_to_tts"]["mode"]       # default comes from the config
        JOB_QUEUE.get_nowait()
        assert _http(port, "GET", f"/api/job/{js['job']}")[0] == 200
        assert _http(port, "GET", f"/api/job/{js['job']}")[2]["state"] == "queued"
    finally:
        srv.shutdown()
        JOBS.clear()


def test_worker_error_handling():
    from imood.service import http_api
    from imood.service.job import Job
    from imood.service.state import JOB_QUEUE
    ran = []

    def fake_run(job):
        ran.append(job.id)
        if job.sessionid == "boom":
            raise RuntimeError("kaboom")
        job.state = "done"
    http_api.run_job = fake_run                                         # the worker thread keeps the fake until the process ends
    threading.Thread(target=http_api.run_job_worker, daemon=True).start()
    bad, good = Job(b"", "boom", "full"), Job(b"", "ok", "full")
    JOB_QUEUE.put(bad); JOB_QUEUE.put(good)
    assert wait_for(lambda: good.state == "done")
    assert (bad.state, bad.error) == ("error", "kaboom") and "RuntimeError: kaboom" in bad.log[-1]
    assert ran == [bad.id, good.id]


class _FakeFramed:
    instances = []

    def __init__(self, host, port):
        self.args, self.calls, self.done = (host, port), [], threading.Event()
        self.done.set()
        _FakeFramed.instances.append(self)

    def open(self, on_status=None):
        self.calls.append(("open",))
        on_status({"state": "received"}); on_status({"state": "playback_ended"})

    def begin(self, session, utt, emotion, text=""):
        self.calls.append(("begin", session, utt, emotion, text))

    def audio_f32(self, b):
        self.calls.append(("audio", len(b)))

    def end(self):
        self.calls.append(("end",))

    def close(self):
        self.calls.append(("close",))


class _FakeEmotion:
    def __init__(self, code, ready):
        self.code, self.ready, self.texts = code, ready, []

    def start(self, text):
        self.texts.append(text)

    def result(self):
        return self.code, self.ready


UTT_STATS = {"first_frame_put": 100.5, "playback_start": 101.0, "playback_end": 103.5, "frames": 75,
             "max_queue_frames": 3, "underruns": 0, "underrun_frames": 0, "overflows": 0, "lt_fps": 25.0}


def _fake_stream_segments(tts, tts_kw, text_q, rs, emit, stat, mark, raw24, sent16):
    while True:
        seg = text_q.get()
        if seg is None:
            break
        stat["tts_segments"] += 1
        x = np.full(2400, 0.2, dtype=np.float32)
        raw24.append(x); stat["tts_chunks"] += 1; mark("tts_first_chunk")
        y = rs.resample_chunk(x)
        sent16.append(y); emit(y.astype("<f4").tobytes())
    y = rs.resample_chunk(np.zeros(0, np.float32), last=True)
    if len(y):
        sent16.append(y); emit(y.astype("<f4").tobytes())


def _run_job_with_fakes(job_mode, emotion, sessionid="sess1", sessions=("sess1",), cfg_override=None, lt=None, transcript="", stream_segments=None, utt_data=None, llm=None):
    """Run service.runner.run_job against fakes. Returns (job, fake_framed_or_None, fake_calls, out_dir)."""
    from imood.service import runner
    from imood.service.job import Job
    from imood.service.state import MODELS, SERVICE_CONFIG
    saved = {k: getattr(runner, k) for k in ("lt_get", "discover_session", "llm_deltas", "_emotion_client", "FramedClient", "SERVICE_CONFIG", "LIVETALKING_CONFIG")}
    saved_stream, saved_tr, saved_load = runner.tts_voice.stream_segments, runner.transcriber.transcribe, runner.cfgmod.load
    saved_models = dict(MODELS)
    fake_calls = []
    tmp_rel = "outputs/_characterization_tmp"
    try:
        MODELS["asr"], MODELS["tts"] = "fake-asr", "fake-tts"

        def lt_get(base, path):
            fake_calls.append(("lt_get", path))
            if path == "/api/admin/sessions":
                return {"data": {"sessions": [{"sessionid": s} for s in sessions]}}
            return {"code": 0, "data": dict(UTT_STATS if utt_data is None else utt_data)}
        runner.lt_get = lt_get
        runner.discover_session = lambda base, avatar: fake_calls.append(("discover", avatar)) or ("found-sess", [])
        runner.transcriber.transcribe = lambda model, wav, a: fake_calls.append(("asr", model, wav, a)) or ("你好呀", 1.5)
        runner.llm_deltas = llm or (lambda cfg, text: iter(["今天心情真的很好呀。", "我們一起出去玩吧！"]))
        runner.tts_voice.stream_segments = stream_segments or _fake_stream_segments
        runner._emotion_client = lambda: emotion
        runner.FramedClient = _FakeFramed
        runner.SERVICE_CONFIG = {**SERVICE_CONFIG, "output_dir": tmp_rel}
        if lt:
            runner.LIVETALKING_CONFIG = lt
        if cfg_override is not None:
            real_load = saved_load
            runner.cfgmod.load = lambda path=None: {**real_load(path), **cfg_override}
        _FakeFramed.instances.clear()
        job = Job(b"RIFFwav", sessionid, job_mode)
        job.transcript = transcript
        try:
            runner.run_job(job)
            err = None
        except Exception as e:
            err = e
        return job, err, (_FakeFramed.instances[:] or [None])[0], fake_calls, ROOT / tmp_rel / job.id
    finally:
        for k, v in saved.items():
            setattr(runner, k, v)
        runner.tts_voice.stream_segments, runner.transcriber.transcribe, runner.cfgmod.load = saved_stream, saved_tr, saved_load
        MODELS.clear(); MODELS.update(saved_models)


def test_run_job_full_mode_framed():
    emo = _FakeEmotion(4, True)
    job, err, fc, calls, out = _run_job_with_fakes("full", emo)
    try:
        assert err is None and job.state == "done" and job.error is None
        assert job.transcript == "你好呀" and job.reply == "今天心情真的很好呀。我們一起出去玩吧！"
        assert job.segments == [job.reply]                                # full mode: the whole reply is one TTS segment
        assert emo.texts == [job.reply]                                   # emotion starts with the first segment text
        assert job.metrics["emotion"] == {"code": 4, "vendor": "happy", "bert_ready": True}
        assert job.metrics["input_audio_s"] == 1.5 and job.metrics["downstream_status"] == ["received", "playback_ended"]
        assert job.metrics["livetalking"] == UTT_STATS and job.metrics["avatar_spoke_s"] == 2.5
        assert (job.metrics["tts_segments"], job.metrics["tts_chunks"]) == (1, 1)
        assert job.metrics["lt_frames"] == 75 and job.metrics["lt_fps"] == 25.0
        assert job.metrics["audio_s_tts_24k"] == 0.1 and job.metrics["audio_s_sent_16k"] == 0.1
        assert job.metrics["output_dir"] == f"outputs/_characterization_tmp/{job.id}"
        for key in ("asr_done", "first_segment_ready", "llm_first_token", "llm_done", "tts_first_chunk", "tts_done",
                    "first_audio_submitted", "audio_stream_closed", "first_frame_put_livetalking",
                    "avatar_speaking_start", "avatar_speaking_end"):
            assert key in job.timestamps, key
        assert fc.args == ("127.0.0.1", 8100)
        kinds = [c[0] for c in fc.calls]
        assert kinds[0] == "open" and kinds[1] == "begin" and kinds[-2:] == ["end", "close"] and set(kinds[2:-2]) == {"audio"}
        assert fc.calls[1] == ("begin", "sess1", job.id, "happy", job.reply)
        assert ("lt_get", "/api/admin/sessions") in calls and ("lt_get", f"/cyber_gf/utt/{job.id}") in calls
        assert not any(c[0] == "discover" for c in calls)
        assert (out / "question.wav").read_bytes() == b"RIFFwav"
        assert (out / "transcript.txt").read_text(encoding="utf-8") == "你好呀\n"
        assert (out / "reply.txt").read_text(encoding="utf-8") == job.reply + "\n"
        assert json.loads((out / "timings.json").read_text(encoding="utf-8"))["state"] in ("speaking", "done")
        assert (out / "reply_24k_tts.wav").exists() and (out / "reply_16k_sent.wav").exists()
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_sentence_mode_and_emotion_not_ready():
    emo = _FakeEmotion(3, False)
    job, err, fc, calls, out = _run_job_with_fakes("sentence", emo, sessionid="")
    try:
        assert err is None and job.state == "done"
        assert job.sessionid == "found-sess" and ("discover", "wav2lip256_avatar3_WA3") in calls
        assert job.segments == ["今天心情真的很好呀。", "我們一起出去玩吧！"] and emo.texts == [job.segments[0]]
        assert job.metrics["emotion"] == {"code": 3, "vendor": "sad", "bert_ready": False}
        assert job.metrics["tts_segments"] == 2
        assert fc.calls[1] == ("begin", "found-sess", job.id, "sad", job.reply)
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_without_emotion_client():
    job, err, fc, calls, out = _run_job_with_fakes("full", None)
    try:
        assert err is None and job.state == "done" and "emotion" not in job.metrics
        assert fc.calls[1] == ("begin", "sess1", job.id, "", job.reply)
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_known_transcript_skips_asr():
    job, err, fc, calls, out = _run_job_with_fakes("full", None, transcript="已經辨識好的句子")
    try:
        assert err is None and job.state == "done" and job.transcript == "已經辨識好的句子"
        assert not any(c[0] == "asr" for c in calls) and job.metrics["input_audio_s"] == 0.0 and "asr_done" in job.timestamps
        assert (out / "transcript.txt").read_text(encoding="utf-8") == "已經辨識好的句子\n"
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_asr_receives_model_wav_and_config():
    from imood.service.state import VOICE_CONFIG
    job, err, fc, calls, out = _run_job_with_fakes("full", None)
    try:
        assert err is None and ("asr", "fake-asr", b"RIFFwav", VOICE_CONFIG["asr"]) in calls
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_tts_failure_ends_job_with_error():
    def failing_stream(*args):
        raise RuntimeError("boom")
    job, err, fc, calls, out = _run_job_with_fakes("full", None, stream_segments=failing_stream, utt_data={})
    try:
        assert err is None and job.state == "error" and job.error == "TTS: boom"
        assert "RuntimeError: boom" in job.log[-1] and "tts_done" not in job.timestamps
        assert "livetalking" not in job.metrics and job.metrics["tts_chunks"] == 0 and job.metrics["audio_s_tts_24k"] == 0.0
        assert fc.calls == [("open",), ("close",)]                          # nothing was ever sent
        assert (out / "reply.txt").exists() and (out / "timings.json").exists()
        assert json.loads((out / "timings.json").read_text(encoding="utf-8"))["state"] == "error"
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_llm_failure_propagates():
    def failing_llm(cfg, text):
        yield "第一段"
        raise ConnectionError("llm down")
    job, err, fc, calls, out = _run_job_with_fakes("full", None, llm=failing_llm)
    try:
        assert isinstance(err, ConnectionError) and str(err) == "llm down"
        assert job.state == "thinking" and job.reply == "第一段" and job.segments == []
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_playback_stats_progress():
    job, err, fc, calls, out = _run_job_with_fakes("full", None, utt_data={"playback_start": 100.0, "playback_end": 100.5})
    try:
        assert err is None and job.state == "done" and job.metrics["livetalking"] == {"playback_start": 100.0, "playback_end": 100.5}
        assert job.metrics["avatar_spoke_s"] == 0.5 and "first_frame_put_livetalking" not in job.timestamps
        assert job.metrics["lt_frames"] is None and job.metrics["lt_fps"] is None
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


def test_run_job_session_errors():
    try:
        job, err, fc, calls, out = _run_job_with_fakes("full", None, sessionid="gone", sessions=("other",))
        assert isinstance(err, RuntimeError)
        assert str(err) == "LiveTalking session gone is not active - reconnect the avatar page"
        assert job.state == "queued" and fc is None
    finally:
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


class _LtHandler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = b""
        if self.headers.get("Transfer-Encoding") == "chunked":
            while True:
                n = int(self.rfile.readline().strip(), 16)
                if n == 0:
                    self.rfile.readline()
                    break
                body += self.rfile.read(n); self.rfile.readline()
        _LtHandler.seen.append((self.path, self.headers.get("Content-Type"), len(body)))
        out = json.dumps({"code": 0, "data": {"frames": 5}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_run_job_http_transport():
    srv, port = serve_http(_LtHandler)
    _LtHandler.seen = []
    emo = _FakeEmotion(1, True)
    try:
        job, err, fc, calls, out = _run_job_with_fakes(
            "full", emo, cfg_override={"downstream": {"transport": "http"}},
            lt={"url": f"http://127.0.0.1:{port}", "avatar_id": "wav2lip256_avatar3_WA3"})
        assert err is None and fc is None and job.state == "done"
        assert job.metrics["emotion"] == {"code": 1, "vendor": "happy", "bert_ready": True}
        assert wait_for(lambda: len(_LtHandler.seen) >= 2)
        paths = sorted(p for p, _, _ in _LtHandler.seen)
        assert paths == [f"/cyber_gf/audio_stream?sessionid=sess1&utt={job.id}",
                         "/cyber_gf/emotion?sessionid=sess1&emotion=happy"], paths
        audio = [s for s in _LtHandler.seen if s[0].startswith("/cyber_gf/audio_stream")][0]
        assert audio[1] == "application/octet-stream" and audio[2] > 0
    finally:
        srv.shutdown()
        shutil.rmtree(ROOT / "outputs/_characterization_tmp", ignore_errors=True)


# ---------------------------------------------------------------- shared helpers (voice settings, LiveTalking API)

def test_voice_settings():
    from imood.common.voice_settings import load_voice_config
    cfg = load_voice_config()
    voice = json.loads((ROOT / "config/voice.json").read_text(encoding="utf-8"))
    assert cfg["persona"] == (ROOT / voice["llm"]["persona_file"]).read_text(encoding="utf-8").strip()
    assert cfg["ref_text"] == (ROOT / voice["tts"]["ref_text_file"]).read_text(encoding="utf-8").strip()
    assert {k: v for k, v in cfg.items() if k not in ("persona", "ref_text")} == voice
    from imood.service import state
    assert state.VOICE_CONFIG == cfg


class _LtApiHandler(BaseHTTPRequestHandler):
    sessions = []
    posted = []

    def do_GET(self):
        body = json.dumps({"data": {"sessions": _LtApiHandler.sessions}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        _LtApiHandler.posted.append((self.path, self.headers["Content-Type"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        body = b'{"data": true}'
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_livetalking_api():
    from imood.downstream import livetalking_api as api
    srv, port = serve_http(_LtApiHandler)
    base = f"http://127.0.0.1:{port}"
    try:
        _LtApiHandler.sessions = [{"sessionid": "a", "avatar_id": "x"}, {"sessionid": "b", "avatar_id": "y"},
                                  {"sessionid": "c", "avatar_id": "x"}]
        assert api.lt_get(base, "/api/admin/sessions") == {"data": {"sessions": _LtApiHandler.sessions}}
        assert api.lt_post_json(base, "/is_speaking", {"sessionid": "a"}) == {"data": True}
        assert _LtApiHandler.posted == [("/is_speaking", "application/json", {"sessionid": "a"})]
        assert api.discover_session(base, "x") == ("c", _LtApiHandler.sessions)          # newest session of that avatar
        assert api.discover_session(base, "y") == ("b", _LtApiHandler.sessions)
        assert api.discover_session(base, "x", explicit="b") == ("b", _LtApiHandler.sessions)
        for call_args, expected in (((base, "zzz"), api.HOW_TO_GET_SESSION.format(avatar="zzz", url=base)),
                                    ((base, "x", "nope"),
                                     "--session nope is not an active LiveTalking session. Active: ['a', 'b', 'c']")):
            try:
                api.discover_session(*call_args)
                raise AssertionError("expected SystemExit")
            except SystemExit as e:
                assert str(e) == expected
    finally:
        srv.shutdown()
    assert api.HOW_TO_GET_SESSION.startswith("\n  No active LiveTalking WebRTC session for this avatar.\n  1. Make sure LiveTalking is running in Ubuntu-24.04 with --avatar_id {avatar} (port 8010).")


# ---------------------------------------------------------------- launcher, config

def test_launcher_plan_details():
    from imood import launcher
    env = {"TTS_FAST_VENV": "/nonexistent/venv", "HF_HOME": "/hf"}
    cfg = {"emotion": {"enabled": True, "udp_host": "127.0.0.2", "udp_port": 9999}, "livetalking": {"url": "http://h:1"}}
    plan = {s.name: s for s in launcher.build_plan("full", cfg=cfg, env=env)}
    lt, em, vo = plan["livetalking"], plan["emotion"], plan["voice"]
    assert lt.foreground and lt.ready_timeout == 180 and lt.stop_patterns == ("livetalking_cyber_gf.py",)
    assert lt.log.endswith("/logs/start_livetalking.log") and lt.argv[0] == "bash"
    assert em.argv == ["/nonexistent/venv/bin/python", "-m", "imood.emotion.udp_service"]
    assert em.env == {"EMOTION_UDP_HOST": "127.0.0.2", "EMOTION_UDP_PORT": "9999", "HF_HOME": "/hf"}
    assert em.stop_patterns == ("imood.emotion.udp_service",) and em.ready_timeout == 120 and not em.foreground
    assert vo.argv[0] == "/nonexistent/venv/bin/python" and vo.argv[1].endswith("/scripts/wsl/voice_service.py")
    assert vo.ready_timeout == 240 and vo.stop_patterns == ("wsl/voice_service.py",) and vo.env["HF_HOME"] == "/hf"
    assert vo.env["LD_LIBRARY_PATH"].startswith("/nvidia/cublas/lib:/nvidia/cudnn/lib:")


def test_launcher_helpers():
    from imood import launcher

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass
    srv, port = serve_http(H)
    assert launcher._http_ok(f"http://127.0.0.1:{port}/", '"ok": true')() is True
    assert launcher._http_ok(f"http://127.0.0.1:{port}/", "missing")() is False
    srv.shutdown()
    assert launcher._http_ok("http://127.0.0.1:1/", "x")() is False
    assert launcher._kill(("zz_no_such_process_pattern_zz",)) == 0
    try:
        launcher.main(["bogus"])
        raise AssertionError("bad command accepted")
    except SystemExit as e:
        assert str(e) == "usage: python -m imood.launcher start [vendor|full] | stop | status"


def test_config_and_contract_files(tmp=None):
    from imood.common import config
    import tempfile
    cfg = config.load()
    assert set(cfg) == {"mode", "vendor", "emotion", "downstream", "livetalking", "intake", "vad"}
    assert cfg["emotion"] == {"enabled": True, "default": "neutral", "udp_host": "127.0.0.1", "udp_port": 8102, "timeout_ms": 300}
    assert cfg["downstream"] == {"host": "127.0.0.1", "port": 8100, "transport": "framed", "enabled": True}
    assert cfg["intake"] == {"mic": {"enabled": True, "host": "127.0.0.1", "port": 8021}}
    assert cfg["vad"]["end_silence_ms"] == 900 and cfg["vad"]["coalesce_ms"] == 1000 and cfg["livetalking"] == {"url": "http://127.0.0.1:8010"}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.yaml"
        p.write_text("", encoding="utf-8")
        assert config.load(p) == {"mode": "vendor"}
        p.write_text("mode: nope\n", encoding="utf-8")
        try:
            config.load(p)
            raise AssertionError("bad mode accepted")
        except ValueError as e:
            assert str(e) == "mode must be one of ('vendor', 'full'), got 'nope'"

    voice = json.loads((ROOT / "config/voice.json").read_text(encoding="utf-8"))
    assert {"asr", "llm", "tts", "output_dir"} <= set(voice)
    assert {"model", "device", "compute_type", "language", "beam_size", "vad_filter"} == set(voice["asr"])
    assert {"url", "persona_file", "temperature", "top_p", "max_tokens"} == set(voice["llm"])
    assert {"model", "device", "dtype", "attn_implementation", "language", "ref_audio", "ref_text_file"} == set(voice["tts"])
    streaming_config = json.loads((ROOT / "config/streaming_service.json").read_text(encoding="utf-8"))
    assert {"wsl_distro", "service", "tts_fast", "resample", "llm_to_tts", "livetalking", "output_dir"} <= set(streaming_config)
    assert streaming_config["service"] == {"host": "0.0.0.0", "port": 8020} and streaming_config["llm_to_tts"]["mode"] == "full"
    assert {"max_seq_len", "xvec_only", "append_silence", "chunk_size"} == set(streaming_config["tts_fast"])
    assert (streaming_config["resample"]["from_hz"], streaming_config["resample"]["to_hz"]) == (24000, 16000)
    assert {"sentence_end_chars", "min_segment_chars", "max_segment_chars", "comma_chars"} <= set(streaming_config["llm_to_tts"])
    assert {"url", "avatar_id"} == set(streaming_config["livetalking"])
    for rel in ("config/persona.txt", "config/ref_text.txt", "assets/ref.wav", "assets/test.wav"):
        assert (ROOT / rel).exists(), rel


def test_external_interface_names():
    """Public routes and ids that other systems depend on (static check of the sources)."""
    plugin = (ROOT / "imood/downstream/livetalking_plugin.py").read_text(encoding="utf-8")
    routes = re.findall(r'app\.router\.add_(\w+)\("([^"]+)"', plugin)
    assert routes == [("post", "/cyber_gf/audio_stream"), ("get", "/cyber_gf/utt/{utt}"), ("get", "/cyber_gf/stats"),
                      ("post", "/cyber_gf/emotion"), ("get", "/cyber_gf/emotion"), ("get", "/cyber_gf/voice"),
                      ("static", "/imood/")], routes
    html = (ROOT / "web/imood_ai.html").read_text(encoding="utf-8")
    for needle in ("/cyber_gf/emotion?sessionid=", "avatars.json", "/ws/audio", "/api/ask", "/api/job/", "/offer"):
        assert needle in html, needle
    avatars = json.loads((ROOT / "web/avatars.json").read_text(encoding="utf-8"))["avatars"]
    assert [a["id"] for a in avatars] == ["wav2lip256_emo_calm", "wav2lip256_avatar3_WA3", "wav2lip256_avatar3_BA3",
                                          "wav2lip256_avatar3_WA2", "wav2lip256_avatar3_BA2",
                                          "wav2lip256_avatar2_mA1", "wav2lip256_avatar2_mB2"]
    assert all(set(a) == {"id", "label"} for a in avatars)
    for name in ("VENDOR_VOICE_TOKEN", "LEGACY_VOICE", "EMOTION_FADE_FRAMES", "EMOTION_SPEAK"):
        assert any(name in (ROOT / p).read_text(encoding="utf-8") for p in
                   ("imood/intake/vendor_ws.py", "config/paths.env", "imood/downstream/face_switcher.py")), name


# ---------------------------------------------------------------- emotion module interface (names and results only)

def test_emotion_interfaces():
    from imood.common import labels
    from imood.emotion import labels as native
    from imood.emotion.preprocess import HALLUCINATIONS, MIN_CHARS, clean, prepare
    from imood.emotion.udp_client import EmotionClientSync
    from imood.emotion.udp_service import serve

    assert native.NATIVE_LABELS == ["平淡語氣", "關切語調", "開心語調", "憤怒語調", "悲傷語調", "疑問語調", "驚奇語調", "厭惡語調"]
    assert len(native.MODEL_REVISION) == 40
    assert labels.FIVE_LABELS == ["中性", "喜", "怒", "哀", "樂"] and labels.DEFAULT_CODE == 0
    assert labels.FIVE_TO_VENDOR == {0: "neutral", 1: "happy", 2: "angry", 3: "sad", 4: "happy"}
    assert labels.VENDOR_EMOTIONS == ("neutral", "happy", "sad", "angry", "fear")
    assert labels.VENDOR_TO_FACE == {"neutral": "calm", "happy": "happy", "sad": "sad", "angry": "calm", "fear": "calm"}
    assert labels.native_to_code("驚奇語調") == 1 and labels.native_to_code("憤怒語調") == 2

    assert MIN_CHARS == 4 and "謝謝觀看" in HALLUCINATIONS
    assert prepare("  今天天氣不錯  ") == ("今天天氣不錯", "")
    assert prepare("   ") == ("", "空白") and prepare("，。！？ ") == ("", "只有標點")
    assert prepare("謝謝觀看") == ("", "語音辨識靜音幻覺") and prepare("嗯嗯") == ("", "過短（2 字，門檻 4）")
    assert clean("今天天氣不錯") == ("今天天氣不錯", "")

    from imood.emotion import classifier
    assert (classifier.MODEL_ID, classifier.MAX_LENGTH) == ("Johnson8187/Chinese-Emotion-Small", 160)
    assert classifier.resolve_device("cpu") == "cpu"
    clf = classifier.EmotionClassifier("cpu")
    assert (clf.device, clf.model_id, clf.max_length, clf.dtype, clf.model, clf.load_seconds) == \
        ("cpu", classifier.MODEL_ID, 160, "fp32", None, None)
    assert list(classifier.Prediction.__dataclass_fields__) == ["label", "confidence", "probs", "latency_ms"]

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def slow_predict(text):
        time.sleep(0.4)
        return (4, 0.75)
    transport = asyncio.run_coroutine_threadsafe(serve(slow_predict, "127.0.0.1", 18110), loop).result(5)
    try:
        cl = EmotionClientSync("127.0.0.1", 18110, timeout_ms=3000)
        assert cl.result() == (0, False)
        assert cl.start("今天真的很開心呢") is cl
        assert cl.result() == (0, False)                                 # not finished: default, no waiting
        assert wait_for(lambda: cl.result()[1])
        assert cl.result() == (4, True) and cl._conf == 0.75
        blocked = EmotionClientSync("127.0.0.1", 18110, timeout_ms=3000).start("嗯")
        assert wait_for(lambda: blocked.result()[1]) and blocked.result() == (0, True)
        dead = EmotionClientSync("127.0.0.1", 18111, timeout_ms=100).start("今天真的很開心呢")
        assert wait_for(lambda: dead.result()[1]) and dead.result() == (0, True)
    finally:
        loop.call_soon_threadsafe(transport.close)


TESTS = [test_segmenter, test_llm_deltas, test_stream_segments, test_job, test_http_api, test_worker_error_handling,
         test_run_job_full_mode_framed, test_run_job_sentence_mode_and_emotion_not_ready,
         test_run_job_without_emotion_client, test_run_job_known_transcript_skips_asr,
         test_run_job_asr_receives_model_wav_and_config, test_run_job_tts_failure_ends_job_with_error,
         test_run_job_llm_failure_propagates, test_run_job_playback_stats_progress, test_run_job_session_errors, test_run_job_http_transport,
         test_voice_settings, test_livetalking_api, test_launcher_plan_details, test_launcher_helpers, test_config_and_contract_files,
         test_external_interface_names, test_emotion_interfaces]


if __name__ == "__main__":
    for t in TESTS:
        t()
        print("ok", t.__name__)
    print("characterization: ALL PASS")
