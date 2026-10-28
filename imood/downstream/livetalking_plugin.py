"""Project-owned LiveTalking launcher (imood/downstream/livetalking_plugin.py; started via scripts/wsl/livetalking_cyber_gf.py) + continuous-audio adapter.

Runs the UNMODIFIED LiveTalking app.py (same CLI args), but first wraps server.routes.setup_routes so a
few project routes are registered in the same aiohttp app (before upstream's catch-all static '/'):

  POST /cyber_gf/audio_stream?sessionid=..&utt=..   chunked body = continuous 16 kHz mono float32-LE PCM
        -> split into 320-sample (20 ms) frames -> the same AudioFrameData items put_audio_frame() creates,
           enqueued atomically per received block (see put_batch)
           exactly like LiveTalking's own streaming TTS (tts/qwentts.py): 'start' on the first frame of the
           utterance only, then a trailing zero frame carrying 'end'.
  WS   /cyber_gf/voice?token=..   vendor real-time voice receiver (vendor_voice.py; needs env VENDOR_VOICE_TOKEN)
  GET  /cyber_gf/utt/{utt}     per-utterance stats: frames, queue depth, underruns, playback start/end
  GET  /cyber_gf/stats         sessions + queue depth + last LiveTalking infer/final fps
  GET  /imood/...              project frontend (<repo>/web), page /imood/imood_ai.html

Runtime-only instrumentation (no file edits): an instance-level wrapper on avatar_session.asr.get_audio_frame
counts silence frames (type=1 => mouth closes) that occur *inside* an utterance (= underruns), and a
msgqueue registered via avatar_session.add_msgqueue() records when LiveTalking actually plays the
'start'/'end' frames over WebRTC.

Usage (Ubuntu-24.04, LiveTalking venv):  python livetalking_cyber_gf.py <same args as app.py>
"""
import asyncio, collections, json, logging, os, queue, re, runpy, sys, threading, time, uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
LT_DIR = os.environ.get("LT_DIR", "/root/livetalking/LiveTalking")
os.chdir(LT_DIR)
sys.path.insert(0, LT_DIR)
sys.path.insert(0, str(PROJECT))      # repo root: imood package

import numpy as np
from aiohttp import web
import server.routes as lt_routes
from server.session_manager import session_manager
from avatars.base_avatar import AudioFrameData
from utils.logger import logger
from imood.intake.vendor_ws import VendorReceiver
from imood.downstream import framed_receiver
from imood.common import config as imood_cfg
from imood.downstream.face_switcher import EMOTION_PREFIX, EMOTION_AVATARS, EMOTION_SPEAK, EMOTION_FADE, set_emotion, _install_fade, _emo_avatar

FRAME = 320                       # 20 ms @ 16 kHz, as BaseASR.chunk
FRAME_BYTES = FRAME * 4           # float32
OVERFLOW_FRAMES = 50 * 60         # >60 s queued = overflow warning
PREROLL_FRAMES = 24               # 480 ms, all from the first TTS chunk (~0.62 s) -> no added wait
UTTERANCE_STATS = collections.OrderedDict()  # utt -> stats (last 100)
ACTIVE_UTTERANCES = {}                       # sessionid -> stats of the utterance currently being consumed
PLAYBACK_LISTENERS = {}                      # sessionid -> msgqueue thread
LIVETALKING_FPS = {}


class _FpsCapture(logging.Handler):
    rx = re.compile(r"actual avg (infer|final) fps:([\d.]+)")

    def emit(self, record):
        match = self.rx.search(record.getMessage())
        if match:
            LIVETALKING_FPS[match.group(1)] = float(match.group(2)); LIVETALKING_FPS[match.group(1) + "_at"] = time.time()


logger.addHandler(_FpsCapture())


def _record_utterance(utterance_stats):
    UTTERANCE_STATS[utterance_stats["utt"]] = utterance_stats
    while len(UTTERANCE_STATS) > 100:
        UTTERANCE_STATS.popitem(last=False)


def _install_underrun_counter(session_id, session):
    """Wrap this session's ASR.get_audio_frame (instance attribute only) to count in-utterance silence frames."""
    session_asr = session.asr
    if getattr(session_asr, "_cyber_gf_wrapped", False):
        return
    original_get_audio_frame = session_asr.get_audio_frame

    def counting_get_audio_frame():
        frame = original_get_audio_frame()
        utterance_stats = ACTIVE_UTTERANCES.get(session_id)
        if utterance_stats is not None:
            userdata = frame.userdata or {}
            mine = userdata.get("cyber_gf_utt") == utterance_stats["utt"]
            if mine and userdata.get("status") == "start":
                utterance_stats["consumed_start"] = time.time()
            elif utterance_stats.get("consumed_start") and not utterance_stats.get("consumed_end"):
                if frame.type == 1:
                    utterance_stats["underrun_frames"] += 1
                    if not utterance_stats["_in_underrun"]:
                        utterance_stats["underruns"] += 1; utterance_stats["_in_underrun"] = True
                        if len(utterance_stats["underrun_at_s"]) < 20:   # position after the utterance's first consumed frame
                            utterance_stats["underrun_at_s"].append(round(time.time() - utterance_stats["consumed_start"], 3))
                else:
                    utterance_stats["_in_underrun"] = False
            if mine and userdata.get("status") == "end":
                utterance_stats["consumed_end"] = time.time()
        return frame

    session_asr.get_audio_frame = counting_get_audio_frame
    session_asr._cyber_gf_wrapped = True


def _listen_for_playback(session_id, session):
    """Record when LiveTalking actually plays the project's start/end frames (BaseAvatar.notify -> msgqueues)."""
    if session_id in PLAYBACK_LISTENERS:
        return
    message_queue = queue.Queue()
    session.add_msgqueue(message_queue)

    def read_messages():
        while session_manager.get_session(session_id) is session:
            try:
                message = json.loads(message_queue.get(timeout=1))
            except queue.Empty:
                continue
            except Exception:
                continue
            utterance_stats = UTTERANCE_STATS.get(message.get("cyber_gf_utt"))
            if utterance_stats is not None:
                utterance_stats["playback_" + message.get("status", "?")] = time.time()
        PLAYBACK_LISTENERS.pop(session_id, None)

    PLAYBACK_LISTENERS[session_id] = threading.Thread(target=read_messages, daemon=True)
    PLAYBACK_LISTENERS[session_id].start()


def _split_frames(samples):
    return [samples[i * FRAME:(i + 1) * FRAME].copy() for i in range(len(samples) // FRAME)]


async def audio_stream(request):
    session_id = request.query.get("sessionid", "")
    utterance_id = request.query.get("utt") or uuid.uuid4().hex[:12]
    session = session_manager.get_session(session_id)
    if session is None:
        return web.json_response({"code": -1, "msg": "session not found"})
    _install_underrun_counter(session_id, session); _listen_for_playback(session_id, session)
    session_asr = session.asr
    utterance_stats = {"utt": utterance_id, "sessionid": session_id, "request_at": time.time(), "first_frame_put": None, "last_frame_put": None,
          "frames": 0, "bytes": 0, "max_queue_frames": 0, "overflows": 0, "underruns": 0, "underrun_frames": 0, "underrun_at_s": [],
          "_in_underrun": False, "http_chunks": 0}
    _record_utterance(utterance_stats); ACTIVE_UTTERANCES[session_id] = utterance_stats

    state = {"first": True}

    def put_batch(frames, end=False):
        """Enqueue a whole block of 20 ms frames atomically.

        Same item BaseASR.put_audio_frame() creates (AudioFrameData(type=0, userdata=eventpoint)), but all frames
        of the block become visible to the ASR thread at once. MelASR.run_step pulls 32 frames in a tight loop
        with a 10 ms get() timeout; with per-frame put() calls the ASR thread could take the 'start' frame and then
        time out (GIL contention) before the next put(), inserting silence."""
        items = []
        for frame in frames:
            event = {}
            if state["first"]:
                event = {"status": "start", "cyber_gf_utt": utterance_id}; state["first"] = False; utterance_stats["first_frame_put"] = time.time()
            items.append(AudioFrameData(data=frame, type=0, userdata=event))
        if end:
            items.append(AudioFrameData(data=np.zeros(FRAME, np.float32), type=0,
                                        userdata={"status": "end", "cyber_gf_utt": utterance_id}))   # as tts/qwentts.py
        asr_queue = session_asr.queue
        with asr_queue.mutex:
            for item in items:
                asr_queue._put(item); asr_queue.unfinished_tasks += 1
            asr_queue.not_empty.notify_all()
        queue_size = asr_queue.qsize()
        utterance_stats["max_queue_frames"] = max(utterance_stats["max_queue_frames"], queue_size)
        if queue_size > OVERFLOW_FRAMES:
            utterance_stats["overflows"] += 1
        utterance_stats["frames"] += len(frames); utterance_stats["last_frame_put"] = time.time()

    # Pre-roll: the first TTS chunk (~0.62 s after resampling) reaches us as several TCP reads a few ms apart.
    # Releasing the first batch only once PREROLL_FRAMES are buffered keeps MelASR (10 ms get() timeout) from
    # starving between those reads. Costs ~0 latency.
    preroll_frames = int(request.query.get("preroll_frames", PREROLL_FRAMES))
    pending_bytes = b""
    async for chunk in request.content.iter_any():
        utterance_stats["http_chunks"] += 1; utterance_stats["bytes"] += len(chunk)
        pending_bytes += chunk
        frame_count = len(pending_bytes) // FRAME_BYTES
        if frame_count and not (state["first"] and frame_count < preroll_frames):
            put_batch(_split_frames(np.frombuffer(pending_bytes[:frame_count * FRAME_BYTES], dtype="<f4")))
            pending_bytes = pending_bytes[frame_count * FRAME_BYTES:]
    tail_frames = []
    frame_count = len(pending_bytes) // FRAME_BYTES                        # frames still held by the pre-roll (short answers)
    if frame_count:
        tail_frames += _split_frames(np.frombuffer(pending_bytes[:frame_count * FRAME_BYTES], dtype="<f4")); pending_bytes = pending_bytes[frame_count * FRAME_BYTES:]
    if pending_bytes:                                            # partial last frame -> zero-pad
        padded_frame = np.zeros(FRAME, np.float32)
        padded_frame[:len(pending_bytes) // 4] = np.frombuffer(pending_bytes[:len(pending_bytes) // 4 * 4], dtype="<f4")
        tail_frames.append(padded_frame)
    if state["first"] and not tail_frames:                    # no audio at all
        return web.json_response({"code": -1, "msg": "empty audio stream", "data": _public(utterance_stats)})
    put_batch(tail_frames, end=True)
    utterance_stats["eof_at"] = time.time(); utterance_stats["audio_s"] = round(utterance_stats["frames"] * 0.02, 3)
    logger.info("[cyber_gf] utt=%s frames=%d audio=%.2fs max_queue=%d", utterance_id, utterance_stats["frames"], utterance_stats["audio_s"], utterance_stats["max_queue_frames"])
    return web.json_response({"code": 0, "msg": "ok", "data": _public(utterance_stats)})


def _public(utterance_stats):
    return {k: v for k, v in utterance_stats.items() if not k.startswith("_")}


async def utt_status(request):
    utterance_stats = UTTERANCE_STATS.get(request.match_info["utt"])
    if utterance_stats is None:
        return web.json_response({"code": -1, "msg": "unknown utt"})
    session = session_manager.get_session(utterance_stats["sessionid"])
    data = _public(utterance_stats)
    data["queue_frames_now"] = session.asr.queue.qsize() if session is not None else None
    data["lt_fps"] = dict(LIVETALKING_FPS)
    return web.json_response({"code": 0, "msg": "ok", "data": data})


async def stats_route(request):
    sessions = [{"sessionid": session_id, "avatar_id": getattr(getattr(session, "opt", None), "avatar_id", ""),
                 "queue_frames": session.asr.queue.qsize(), "speaking": session.is_speaking()}
                for session_id, session in session_manager.sessions.items() if session is not None]
    return web.json_response({"code": 0, "msg": "ok", "data": {"sessions": sessions, "lt_fps": dict(LIVETALKING_FPS), "cuda_mem_gb": _cuda_mem(),
                              "recent_utts": [_public(utterance) for utterance in list(UTTERANCE_STATS.values())[-5:]]}})


def _cuda_mem():
    """This process's PyTorch CUDA memory (GB): what LiveTalking itself holds, as opposed to the whole WSL VM."""
    try:
        import torch
        bytes_per_gib = 1 << 30
        return {"allocated": round(torch.cuda.memory_allocated() / bytes_per_gib, 2), "reserved": round(torch.cuda.memory_reserved() / bytes_per_gib, 2),
                "max_allocated": round(torch.cuda.max_memory_allocated() / bytes_per_gib, 2), "max_reserved": round(torch.cuda.max_memory_reserved() / bytes_per_gib, 2)}
    except Exception as error:
        return {"error": str(error)}


def _latest_session():
    """The vendor protocol carries no sessionid: play on the most recently opened LiveTalking session."""
    live = [session for session in session_manager.sessions.values() if session is not None]
    return live[-1] if live else None


# ---- emotion -> face. A wav2lip session holds three per-avatar lists (full frames, 256 px faces, paste-back boxes);
# switching emotion swaps all three in place, so the WebRTC connection and the audio queue are untouched.
# Only sessions opened with an emotion avatar (id starts with EMOTION_PREFIX) are switched; other avatars are left alone.
async def emotion_route(request):
    """POST/GET /cyber_gf/emotion?sessionid=..&emotion=happy  (frontend test selector; same switch the vendor path uses)"""
    query = request.query
    session = session_manager.sessions.get(query.get("sessionid", "")) if query.get("sessionid") else _latest_session()
    if session is None:
        return web.json_response({"code": -1, "msg": "session not found"})
    avatar_id = set_emotion(session, query.get("emotion", "neutral"))
    if avatar_id is None:
        return web.json_response({"code": -1, "msg": "session avatar is not an emotion avatar (%s*)" % EMOTION_PREFIX})
    return web.json_response({"code": 0, "msg": "ok", "data": {"avatar_id": avatar_id}})


vendor_receiver = VendorReceiver(_latest_session, lambda data, userdata: AudioFrameData(data=data, type=0, userdata=userdata), on_emotion=set_emotion)

def _find_session(session_id):
    """Framed link: the named session if it exists, else the most recently opened one."""
    if session_id and session_manager.get_session(session_id) is not None:
        return session_id, session_manager.get_session(session_id)
    live = [(key, session) for key, session in session_manager.sessions.items() if session is not None]
    return live[-1] if live else (None, None)


async def _start_framed(app):
    """TCP framed receiver (BEGIN/AUDIO/END, STATUS back) in this process; disabled with downstream.enabled: false."""
    config = imood_cfg.load().get("downstream", {})
    if not config.get("enabled", True):
        return
    handler = framed_receiver.make_handler(audio_stream, _find_session, set_emotion,
                                           lambda utterance_id: _public(UTTERANCE_STATS[utterance_id]) if utterance_id in UTTERANCE_STATS else None)
    app["imood_framed"] = await asyncio.start_server(handler, config.get("bind", "127.0.0.1"), int(config.get("port", 8100)))
    logger.info("[cyber_gf] framed downstream receiver on %s:%s", config.get("bind", "127.0.0.1"), config.get("port", 8100))


_orig_setup_routes = lt_routes.setup_routes


def setup_routes_with_cyber_gf(app):
    # project routes first: upstream registers a catch-all static '/' at the end of setup_routes
    app.router.add_post("/cyber_gf/audio_stream", audio_stream)
    app.router.add_get("/cyber_gf/utt/{utt}", utt_status)
    app.router.add_get("/cyber_gf/stats", stats_route)
    app.router.add_post("/cyber_gf/emotion", emotion_route)
    app.router.add_get("/cyber_gf/emotion", emotion_route)
    app.router.add_get("/cyber_gf/voice", vendor_receiver.handle)     # vendor real-time voice WebSocket (docs/vendor_voice_receiver.md)
    app.router.add_static("/imood/", path=str(PROJECT / "web"), show_index=False)
    app.on_startup.append(_start_framed)
    logger.info("[cyber_gf] adapter routes registered: /cyber_gf/audio_stream, /cyber_gf/utt/{utt}, /cyber_gf/stats, /imood/")
    _orig_setup_routes(app)


lt_routes.setup_routes = setup_routes_with_cyber_gf   # app.py does `from server.routes import setup_routes` at import

if __name__ == "__main__":
    sys.argv = ["app.py"] + sys.argv[1:]
    runpy.run_path(os.path.join(LT_DIR, "app.py"), run_name="__main__")
