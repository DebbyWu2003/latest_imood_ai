"""Run one job: wav -> ASR -> LLM (streamed) -> TTS (resampled) -> LiveTalking, with BERT emotion in parallel.

Pieces: intake/transcriber (ASR), dialogue/ (LLM, segmenter), voice/tts_voice (TTS).
Emotion: the reply text goes to the BERT UDP service at the same time as to TTS; at the first audio chunk the result is
used if it is ready, otherwise the default (never waited for).
"""
import http.client, json, queue, threading, time, traceback, urllib.request

from imood.common import config as cfgmod
from imood.common.labels import code_to_vendor
from imood.dialogue.llm_client import llm_deltas
from imood.dialogue.segmenter import Segmenter
from imood.downstream.framed_client import FramedClient
from imood.downstream.livetalking_api import discover_session, lt_get
from imood.emotion.udp_client import EmotionClientSync
from imood.intake import transcriber
from imood.service.state import LIVETALKING_CONFIG, MODELS, ROOT, SERVICE_CONFIG, VOICE_CONFIG
from imood.voice import tts_voice


def _emotion_client():
    c = cfgmod.load().get("emotion", {})
    if not c.get("enabled", True):
        return None
    return EmotionClientSync(c.get("udp_host", "127.0.0.1"), int(c.get("udp_port", 8102)), int(c.get("timeout_ms", 300)))


def _switch_face(base, sessionid, vendor_emotion):
    """transport http only: ask the LiveTalking adapter to change the face (the framed link carries the emotion in BEGIN)."""
    try:
        req = urllib.request.Request(f"{base}/cyber_gf/emotion?sessionid={sessionid}&emotion={vendor_emotion}", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def _prepare_output_dir(job):
    """Create this job's output directory and store the question audio in it."""
    output_dir = ROOT / SERVICE_CONFIG["output_dir"] / job.id
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "question.wav").write_bytes(job.wav)
    return output_dir


def _resolve_session(job, base):
    """Use the session from the page, else discover one; raise when the named session is not active."""
    if not job.sessionid:
        try:
            job.sessionid, _ = discover_session(base, LIVETALKING_CONFIG["avatar_id"])
        except SystemExit as e:
            raise RuntimeError(str(e).strip())
    else:
        active = [s["sessionid"] for s in lt_get(base, "/api/admin/sessions")["data"]["sessions"]]
        if job.sessionid not in active:
            raise RuntimeError(f"LiveTalking session {job.sessionid} is not active - reconnect the avatar page")


def _transcribe(job, asr_config):
    """Speech recognition unless the transcript is already known (microphone path: recognised and merged upstream)."""
    job.state = "asr"
    if job.transcript:
        duration = 0.0
    else:
        job.transcript, duration = transcriber.transcribe(MODELS["asr"], job.wav, asr_config)
    job.mark("asr_done"); job.metrics["input_audio_s"] = round(duration, 2)


class _AudioPipeline:
    """Reply text -> TTS (producer thread) -> audio queue -> LiveTalking (sender thread).

    The reply text also goes to the emotion service at the same moment as to TTS; the first audio chunk takes the
    emotion result if it is ready, otherwise the default (never waited for).
    """

    def __init__(self, job, base, tts_config, tts_fast_config):
        import soxr
        self.job, self.base = job, base
        self.utterance_id = job.id
        self.text_queue, self.audio_queue = queue.Queue(), queue.Queue()
        self.resampler = soxr.ResampleStream(SERVICE_CONFIG["resample"]["from_hz"], SERVICE_CONFIG["resample"]["to_hz"], 1, dtype="float32")
        self.raw_24k_chunks, self.sent_16k_chunks = [], []
        self.stats = {"tts_chunks": 0, "audio_q_max": 0, "tts_segments": 0}
        self.tts_kwargs = dict(language=tts_config["language"], ref_audio=str(ROOT / tts_config["ref_audio"]), ref_text=VOICE_CONFIG["ref_text"],
                               xvec_only=tts_fast_config["xvec_only"], append_silence=tts_fast_config["append_silence"],
                               chunk_size=tts_fast_config["chunk_size"])
        self.downstream_config = cfgmod.load().get("downstream", {})
        self.framed = self.downstream_config.get("transport", "framed") == "framed" and self.downstream_config.get("enabled", True)
        self.emotion_state = {"client": _emotion_client(), "decided": False, "started": False}
        self.sender_result = {}
        self.producer_thread = None
        self.sender_thread = None

    def start(self):
        self.producer_thread = threading.Thread(target=self._produce_audio, daemon=True)
        self.sender_thread = threading.Thread(target=self._send_audio, daemon=True)
        self.producer_thread.start(); self.sender_thread.start()

    def submit_segment(self, text):
        emotion_state = self.emotion_state
        if emotion_state["client"] is not None and not emotion_state["started"]:          # reply text goes to BERT and TTS at the same moment
            emotion_state["started"] = True
            emotion_state["client"].start(text)
        self.text_queue.put(text)

    def _enqueue_audio(self, chunk):
        emotion_state = self.emotion_state
        if not emotion_state["decided"]:                          # first audio chunk: take BERT's answer if ready, else the default
            emotion_state["decided"] = True
            if emotion_state["client"] is not None:
                code, ready = emotion_state["client"].result()
                vendor = code_to_vendor(code)
                self.job.metrics["emotion"] = {"code": code, "vendor": vendor, "bert_ready": ready}
                if not self.framed:                                  # framed link carries the emotion in BEGIN
                    threading.Thread(target=_switch_face, args=(self.base, self.job.sessionid, vendor), daemon=True).start()
        self.audio_queue.put(chunk)
        self.stats["audio_q_max"] = max(self.stats["audio_q_max"], self.audio_queue.qsize())

    def _produce_audio(self):
        job = self.job
        try:
            tts_voice.stream_segments(MODELS["tts"], self.tts_kwargs, self.text_queue, self.resampler, self._enqueue_audio,
                                      self.stats, job.mark, self.raw_24k_chunks, self.sent_16k_chunks)
            job.mark("tts_done")
        except Exception as e:
            job.error = f"TTS: {e}"; job.log.append(traceback.format_exc())
        finally:
            self.audio_queue.put(None)

    def _audio_chunks(self):
        while True:
            chunk = self.audio_queue.get()
            if chunk is None:
                return
            self.job.mark("first_audio_submitted")
            yield chunk

    def _send_http(self):
        job = self.job
        conn = http.client.HTTPConnection(self.base.split("//")[1], timeout=300)
        conn.request("POST", f"/cyber_gf/audio_stream?sessionid={job.sessionid}&utt={self.utterance_id}", body=self._audio_chunks(),
                     headers={"Content-Type": "application/octet-stream"}, encode_chunked=True)
        self.sender_result.update(json.loads(conn.getresponse().read()))
        job.mark("audio_stream_closed")

    def _send_framed(self):
        job, config = self.job, self.downstream_config
        client = FramedClient(config.get("host", "127.0.0.1"), config.get("port", 8100))
        client.open(on_status=lambda status: job.metrics.setdefault("downstream_status", []).append(status.get("state")))
        first = True
        try:
            while True:
                chunk = self.audio_queue.get()
                if chunk is None:
                    break
                if first:                                   # emotion was decided before the first chunk was queued
                    first = False
                    job.mark("first_audio_submitted")
                    client.begin(job.sessionid, self.utterance_id, (job.metrics.get("emotion") or {}).get("vendor", ""), text=job.reply)
                client.audio_f32(chunk)
            if not first:
                client.end()
                client.done.wait(180)
                job.mark("audio_stream_closed")
        finally:
            client.close()

    def _send_audio(self):
        try:
            (self._send_framed if self.framed else self._send_http)()
        except Exception as e:
            self.job.error = f"send: {e}"; self.job.log.append(traceback.format_exc())


def _stream_reply(job, pipeline):
    """LLM (streamed) -> segments -> TTS. Sentence mode hands every finished sentence over at once, full mode the whole reply."""
    job.state = "thinking"
    segmenter = Segmenter(SERVICE_CONFIG["llm_to_tts"])
    sentence_mode = job.mode == "sentence"
    for delta in llm_deltas(VOICE_CONFIG, job.transcript):
        job.mark("llm_first_token")
        job.reply += delta
        if sentence_mode:
            for segment in segmenter.feed(delta):
                job.segments.append(segment); job.mark("first_segment_ready"); pipeline.submit_segment(segment); job.state = "synthesizing"
    job.mark("llm_done")
    remaining = segmenter.flush() if sentence_mode else [job.reply]
    for segment in remaining:
        if segment:
            job.segments.append(segment); job.mark("first_segment_ready"); pipeline.submit_segment(segment)
    pipeline.text_queue.put(None)
    job.state = "synthesizing"
    assert "".join(job.segments) == job.reply, "segmentation must preserve the exact reply text"


def _await_playback(job, pipeline, base):
    """Wait for playback start / end (reported by the LiveTalking adapter), then join the worker threads."""
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            response = lt_get(base, f"/cyber_gf/utt/{pipeline.utterance_id}")
        except Exception:
            response = {}
        stats = response.get("data") or {}
        if stats.get("first_frame_put"):
            job.mark("first_frame_put_livetalking", stats["first_frame_put"])
        if stats.get("playback_start"):
            job.mark("avatar_speaking_start", stats["playback_start"]); job.state = "speaking"
        if stats.get("playback_end"):
            job.mark("avatar_speaking_end", stats["playback_end"]); job.metrics["livetalking"] = stats; break
        if job.error and not pipeline.producer_thread.is_alive() and not pipeline.sender_thread.is_alive():
            break
        time.sleep(0.05)
    pipeline.producer_thread.join(5); pipeline.sender_thread.join(5)


def _save_results(job, pipeline, output_dir):
    """Write the audio and text results, fill in the metrics and set the final job state."""
    import numpy as np, soundfile as sf
    audio_16k = np.concatenate(pipeline.sent_16k_chunks) if pipeline.sent_16k_chunks else np.zeros(0, np.float32)
    audio_24k = np.concatenate(pipeline.raw_24k_chunks) if pipeline.raw_24k_chunks else np.zeros(0, np.float32)
    sf.write(str(output_dir / "reply_24k_tts.wav"), audio_24k, 24000)
    sf.write(str(output_dir / "reply_16k_sent.wav"), audio_16k, 16000)
    livetalking_stats = job.metrics.get("livetalking") or pipeline.sender_result.get("data") or {}
    job.metrics.update({
        "tts_chunks": pipeline.stats["tts_chunks"], "tts_segments": pipeline.stats["tts_segments"], "service_audio_queue_max": pipeline.stats["audio_q_max"],
        "audio_s_tts_24k": round(len(audio_24k) / 24000, 3), "audio_s_sent_16k": round(len(audio_16k) / 16000, 3),
        "lt_frames": livetalking_stats.get("frames"), "lt_max_queue_frames": livetalking_stats.get("max_queue_frames"),
        "lt_underruns": livetalking_stats.get("underruns"), "lt_underrun_frames": livetalking_stats.get("underrun_frames"),
        "lt_overflows": livetalking_stats.get("overflows"), "lt_fps": livetalking_stats.get("lt_fps"),
        "tts_rtf": round((job.timestamps.get("tts_done", 0) - job.timestamps.get("first_segment_ready", 0)) / max(len(audio_24k) / 24000, 1e-6), 3),
    })
    if "avatar_speaking_start" in job.timestamps and "avatar_speaking_end" in job.timestamps:
        job.metrics["avatar_spoke_s"] = round(job.timestamps["avatar_speaking_end"] - job.timestamps["avatar_speaking_start"], 3)
    (output_dir / "transcript.txt").write_text(job.transcript + "\n", encoding="utf-8")
    (output_dir / "reply.txt").write_text(job.reply + "\n", encoding="utf-8")
    job.state = "error" if job.error else "done"
    (output_dir / "timings.json").write_text(json.dumps(job.info(), ensure_ascii=False, indent=2), encoding="utf-8")
    job.metrics["output_dir"] = str(output_dir.relative_to(ROOT))


def run_job(job):
    asr_config, tts_config, tts_fast_config = VOICE_CONFIG["asr"], VOICE_CONFIG["tts"], SERVICE_CONFIG["tts_fast"]
    output_dir = _prepare_output_dir(job)
    base = LIVETALKING_CONFIG["url"]
    _resolve_session(job, base)
    _transcribe(job, asr_config)

    pipeline = _AudioPipeline(job, base, tts_config, tts_fast_config)
    pipeline.start()
    _stream_reply(job, pipeline)
    _await_playback(job, pipeline, base)
    _save_results(job, pipeline, output_dir)
