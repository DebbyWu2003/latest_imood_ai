"""Project-owned LiveTalking launcher + continuous-audio adapter (Step 12 v2).

Runs the UNMODIFIED LiveTalking app.py (same CLI args), but first wraps server.routes.setup_routes so a
few project routes are registered in the same aiohttp app (before upstream's catch-all static '/'):

  POST /cyber_gf/audio_stream?sessionid=..&utt=..   chunked body = continuous 16 kHz mono float32-LE PCM
        -> split into 320-sample (20 ms) frames -> the same AudioFrameData items put_audio_frame() creates,
           enqueued atomically per received block (see put_batch)
           exactly like LiveTalking's own streaming TTS (tts/qwentts.py): 'start' on the first frame of the
           utterance only, then a trailing zero frame carrying 'end'.
  GET  /cyber_gf/utt/{utt}     per-utterance stats: frames, queue depth, underruns, playback start/end
  GET  /cyber_gf/stats         sessions + queue depth + last LiveTalking infer/final fps
  GET  /imood/...              project frontend (C:/.../cyber_gf/web), page /imood/imood_ai.html

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

import numpy as np
from aiohttp import web
import server.routes as lt_routes
from server.session_manager import session_manager
from avatars.base_avatar import AudioFrameData
from utils.logger import logger

FRAME = 320                       # 20 ms @ 16 kHz, as BaseASR.chunk
FRAME_BYTES = FRAME * 4           # float32
OVERFLOW_FRAMES = 50 * 60         # >60 s queued = overflow warning
PREROLL_FRAMES = 24               # 480 ms, all from the first TTS chunk (~0.62 s) -> no added wait
UTTS = collections.OrderedDict()  # utt -> stats (last 100)
ACTIVE = {}                       # sessionid -> stats of the utterance currently being consumed
LISTENERS = {}                    # sessionid -> msgqueue thread
FPS = {}


class _FpsCapture(logging.Handler):
    rx = re.compile(r"actual avg (infer|final) fps:([\d.]+)")

    def emit(self, record):
        m = self.rx.search(record.getMessage())
        if m:
            FPS[m.group(1)] = float(m.group(2)); FPS[m.group(1) + "_at"] = time.time()


logger.addHandler(_FpsCapture())


def _remember(st):
    UTTS[st["utt"]] = st
    while len(UTTS) > 100:
        UTTS.popitem(last=False)


def _instrument(sid, sess):
    """Wrap this session's ASR.get_audio_frame (instance attribute only) to count in-utterance silence frames."""
    asr = sess.asr
    if getattr(asr, "_cyber_gf_wrapped", False):
        return
    orig = asr.get_audio_frame

    def wrapped():
        f = orig()
        st = ACTIVE.get(sid)
        if st is not None:
            ud = f.userdata or {}
            mine = ud.get("cyber_gf_utt") == st["utt"]
            if mine and ud.get("status") == "start":
                st["consumed_start"] = time.time()
            elif st.get("consumed_start") and not st.get("consumed_end"):
                if f.type == 1:
                    st["underrun_frames"] += 1
                    if not st["_in_underrun"]:
                        st["underruns"] += 1; st["_in_underrun"] = True
                        if len(st["underrun_at_s"]) < 20:   # position after the utterance's first consumed frame
                            st["underrun_at_s"].append(round(time.time() - st["consumed_start"], 3))
                else:
                    st["_in_underrun"] = False
            if mine and ud.get("status") == "end":
                st["consumed_end"] = time.time()
        return f

    asr.get_audio_frame = wrapped
    asr._cyber_gf_wrapped = True


def _listen(sid, sess):
    """Record when LiveTalking actually plays our start/end frames (BaseAvatar.notify -> msgqueues)."""
    if sid in LISTENERS:
        return
    q = queue.Queue()
    sess.add_msgqueue(q)

    def loop():
        while session_manager.get_session(sid) is sess:
            try:
                msg = json.loads(q.get(timeout=1))
            except queue.Empty:
                continue
            except Exception:
                continue
            st = UTTS.get(msg.get("cyber_gf_utt"))
            if st is not None:
                st["playback_" + msg.get("status", "?")] = time.time()
        LISTENERS.pop(sid, None)

    LISTENERS[sid] = threading.Thread(target=loop, daemon=True)
    LISTENERS[sid].start()


async def audio_stream(request):
    sid = request.query.get("sessionid", "")
    utt = request.query.get("utt") or uuid.uuid4().hex[:12]
    sess = session_manager.get_session(sid)
    if sess is None:
        return web.json_response({"code": -1, "msg": "session not found"})
    _instrument(sid, sess); _listen(sid, sess)
    asr = sess.asr
    st = {"utt": utt, "sessionid": sid, "request_at": time.time(), "first_frame_put": None, "last_frame_put": None,
          "frames": 0, "bytes": 0, "max_queue_frames": 0, "overflows": 0, "underruns": 0, "underrun_frames": 0, "underrun_at_s": [],
          "_in_underrun": False, "http_chunks": 0}
    _remember(st); ACTIVE[sid] = st

    state = {"first": True}

    def put_batch(frames, end=False):
        """Enqueue a whole block of 20 ms frames atomically.

        Same item BaseASR.put_audio_frame() creates (AudioFrameData(type=0, userdata=eventpoint)), but all frames
        of the block become visible to the ASR thread at once. MelASR.run_step pulls 32 frames in a tight loop
        with a 10 ms get() timeout; with per-frame put() calls the ASR thread could take the 'start' frame and then
        time out (GIL contention) before the next put(), inserting silence (measured: 2 frames at +0.01 s)."""
        items = []
        for f in frames:
            ev = {}
            if state["first"]:
                ev = {"status": "start", "cyber_gf_utt": utt}; state["first"] = False; st["first_frame_put"] = time.time()
            items.append(AudioFrameData(data=f, type=0, userdata=ev))
        if end:
            items.append(AudioFrameData(data=np.zeros(FRAME, np.float32), type=0,
                                        userdata={"status": "end", "cyber_gf_utt": utt}))   # as tts/qwentts.py
        q = asr.queue
        with q.mutex:
            for it in items:
                q._put(it); q.unfinished_tasks += 1
            q.not_empty.notify_all()
        n = q.qsize()
        st["max_queue_frames"] = max(st["max_queue_frames"], n)
        if n > OVERFLOW_FRAMES:
            st["overflows"] += 1
        st["frames"] += len(frames); st["last_frame_put"] = time.time()

    def split(pcm):
        return [pcm[i * FRAME:(i + 1) * FRAME].copy() for i in range(len(pcm) // FRAME)]

    # Pre-roll: the first TTS chunk (~0.62 s after resampling) reaches us as several TCP reads a few ms apart.
    # Releasing the first batch only once PREROLL_FRAMES are buffered keeps MelASR (10 ms get() timeout) from
    # starving between those reads (measured: 1 silence frame at +0.01 s without it). Costs ~0 latency.
    preroll = int(request.query.get("preroll_frames", PREROLL_FRAMES))
    buf = b""
    async for data in request.content.iter_any():
        st["http_chunks"] += 1; st["bytes"] += len(data)
        buf += data
        n = len(buf) // FRAME_BYTES
        if n and not (state["first"] and n < preroll):
            put_batch(split(np.frombuffer(buf[:n * FRAME_BYTES], dtype="<f4")))
            buf = buf[n * FRAME_BYTES:]
    tail = []
    n = len(buf) // FRAME_BYTES                        # frames still held by the pre-roll (short answers)
    if n:
        tail += split(np.frombuffer(buf[:n * FRAME_BYTES], dtype="<f4")); buf = buf[n * FRAME_BYTES:]
    if buf:                                            # partial last frame -> zero-pad
        pad = np.zeros(FRAME, np.float32)
        pad[:len(buf) // 4] = np.frombuffer(buf[:len(buf) // 4 * 4], dtype="<f4")
        tail.append(pad)
    if state["first"] and not tail:                    # no audio at all
        return web.json_response({"code": -1, "msg": "empty audio stream", "data": _public(st)})
    put_batch(tail, end=True)
    st["eof_at"] = time.time(); st["audio_s"] = round(st["frames"] * 0.02, 3)
    logger.info("[cyber_gf] utt=%s frames=%d audio=%.2fs max_queue=%d", utt, st["frames"], st["audio_s"], st["max_queue_frames"])
    return web.json_response({"code": 0, "msg": "ok", "data": _public(st)})


def _public(st):
    return {k: v for k, v in st.items() if not k.startswith("_")}


async def utt_status(request):
    st = UTTS.get(request.match_info["utt"])
    if st is None:
        return web.json_response({"code": -1, "msg": "unknown utt"})
    sess = session_manager.get_session(st["sessionid"])
    data = _public(st)
    data["queue_frames_now"] = sess.asr.queue.qsize() if sess is not None else None
    data["lt_fps"] = dict(FPS)
    return web.json_response({"code": 0, "msg": "ok", "data": data})


async def stats(request):
    sessions = [{"sessionid": sid, "avatar_id": getattr(getattr(s, "opt", None), "avatar_id", ""),
                 "queue_frames": s.asr.queue.qsize(), "speaking": s.is_speaking()}
                for sid, s in session_manager.sessions.items() if s is not None]
    return web.json_response({"code": 0, "msg": "ok", "data": {"sessions": sessions, "lt_fps": dict(FPS), "cuda_mem_gb": _cuda_mem(),
                              "recent_utts": [_public(u) for u in list(UTTS.values())[-5:]]}})


def _cuda_mem():
    """This process's PyTorch CUDA memory (GB): what LiveTalking itself holds, as opposed to the whole WSL VM."""
    try:
        import torch
        g = 1 << 30
        return {"allocated": round(torch.cuda.memory_allocated() / g, 2), "reserved": round(torch.cuda.memory_reserved() / g, 2),
                "max_allocated": round(torch.cuda.max_memory_allocated() / g, 2), "max_reserved": round(torch.cuda.max_memory_reserved() / g, 2)}
    except Exception as e:
        return {"error": str(e)}


_orig_setup_routes = lt_routes.setup_routes


def setup_routes_with_cyber_gf(app):
    # project routes first: upstream registers a catch-all static '/' at the end of setup_routes
    app.router.add_post("/cyber_gf/audio_stream", audio_stream)
    app.router.add_get("/cyber_gf/utt/{utt}", utt_status)
    app.router.add_get("/cyber_gf/stats", stats)
    app.router.add_static("/imood/", path=str(PROJECT / "web"), show_index=False)
    logger.info("[cyber_gf] adapter routes registered: /cyber_gf/audio_stream, /cyber_gf/utt/{utt}, /cyber_gf/stats, /imood/")
    _orig_setup_routes(app)


lt_routes.setup_routes = setup_routes_with_cyber_gf   # app.py does `from server.routes import setup_routes` at import

if __name__ == "__main__":
    sys.argv = ["app.py"] + sys.argv[1:]
    runpy.run_path(os.path.join(LT_DIR, "app.py"), run_name="__main__")
