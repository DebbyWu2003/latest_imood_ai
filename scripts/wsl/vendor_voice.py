"""Vendor real-time voice receiver (WebSocket server), see docs/vendor_voice_receiver.md.

The vendor's sender (browser page) connects here and pushes, per utterance:
  text  {"type":"utterance.start", utterance_id, emotion, format, sample_rate, channels, ...}
  bin   20 ms PCM s16le 16 kHz mono chunks (640 bytes)
  text  {"type":"utterance.end", utterance_id, chunks, aborted}
Audio is converted to the 20 ms float32 frames LiveTalking's ASR queue expects (same items as
livetalking_cyber_gf.audio_stream) and the emotion is announced to /sse listeners the moment that
utterance actually starts playing:  {"type":"emotion","utterance_id":..,"emotion":..}   (docs/emotion_interface.md)

Kept free of LiveTalking imports: the launcher injects `get_session` and `make_item`, so the protocol
logic can be tested without a GPU (scripts/wsl/vendor_voice_test.py).
"""
import asyncio, hmac, json, logging, os, queue, uuid

import numpy as np
from aiohttp import WSMsgType, web

log = logging.getLogger("vendor_voice")

FRAME = 320                      # 20 ms @ 16 kHz
FRAME_BYTES = FRAME * 2          # s16le
PREROLL_FRAMES = int(os.environ.get("VENDOR_PREROLL_FRAMES", 24))   # 480 ms, as the existing audio_stream
BLOCK_FRAMES = int(os.environ.get("VENDOR_BLOCK_FRAMES", 10))       # jitter buffer for real-time senders (200 ms)
EMOTIONS = {"neutral", "happy", "sad", "angry", "fear"}
DEFAULT_ORIGINS = {"https://websocket-audio-tester.cielsi.workers.dev"}


class _Utt:
    def __init__(self, utt_id, emotion, sess):
        self.id, self.emotion, self.sess = utt_id, emotion, sess
        self.buf = b""
        self.started = False       # 'start' frame already enqueued
        self.frames = 0
        self.chunks = 0


class VendorReceiver:
    def __init__(self, get_session, make_item, token=None, origins=None):
        self.get_session = get_session          # () -> avatar session or None
        self.make_item = make_item              # (float32 ndarray, userdata dict) -> item for sess.asr.queue
        self.token = token if token is not None else os.environ.get("VENDOR_VOICE_TOKEN", "")
        extra = {o for o in os.environ.get("VENDOR_ALLOWED_ORIGINS", "").split(",") if o}
        self.origins = set(origins) if origins is not None else DEFAULT_ORIGINS | extra
        self.current = None                     # the one active sender websocket
        if not self.token:
            log.warning("VENDOR_VOICE_TOKEN is not set: every connection will be rejected with 4001")

    # ---- helpers -----------------------------------------------------------------------------
    async def _send(self, ws, obj):
        if not ws.closed:
            try:
                await ws.send_json(obj)
            except ConnectionError:
                pass

    def _enqueue(self, sess, frames, first_meta=None, end_meta=None):
        """Put a block of float32 frames on the ASR queue atomically (same reasoning as put_batch in
        livetalking_cyber_gf.py): all frames become visible at once so MelASR's 10 ms get() cannot starve."""
        items = []
        for i, f in enumerate(frames):
            items.append(self.make_item(f, dict(first_meta) if (i == 0 and first_meta) else {}))
        if end_meta is not None:
            items.append(self.make_item(np.zeros(FRAME, np.float32), dict(end_meta)))
        q = sess.asr.queue
        with q.mutex:
            for it in items:
                q._put(it); q.unfinished_tasks += 1
            q.not_empty.notify_all()

    @staticmethod
    def _to_frames(raw):
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return [pcm[i * FRAME:(i + 1) * FRAME].copy() for i in range(len(pcm) // FRAME)]

    def _meta(self, utt, status):
        return {"status": status, "vendor_utt": utt.id, "emotion": utt.emotion}

    def _flush(self, utt, final=False):
        """Release buffered audio: first after PREROLL_FRAMES, then in BLOCK_FRAMES blocks; everything on final."""
        n = len(utt.buf) // FRAME_BYTES
        if not final and n < (BLOCK_FRAMES if utt.started else PREROLL_FRAMES):
            return
        frames = self._to_frames(utt.buf[:n * FRAME_BYTES])
        rest = utt.buf[n * FRAME_BYTES:]
        utt.buf = b""
        if final and len(rest) >= 2:                           # zero-pad the last partial frame
            pad = np.zeros(FRAME, np.float32)
            t = np.frombuffer(rest[:len(rest) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            pad[:len(t)] = t
            frames.append(pad)
        elif not final:
            utt.buf = rest
        if not frames:
            return
        first = None if utt.started else self._meta(utt, "start")
        utt.started = True
        self._enqueue(utt.sess, frames, first_meta=first, end_meta=self._meta(utt, "end") if final else None)
        utt.frames += len(frames)

    # ---- main handler ------------------------------------------------------------------------
    async def handle(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024, compress=False)   # no permessage-deflate: PCM gains little
        await ws.prepare(request)

        token = request.query.get("token", "")
        origin = request.headers.get("Origin")
        if not self.token or not hmac.compare_digest(token.encode(), self.token.encode()) \
                or (origin is not None and origin not in self.origins):
            log.warning("auth failed (origin=%s)", origin)
            await ws.close(code=4001, message=b"auth failed")
            return ws

        old = self.current                     # only after auth, so a failed attempt never kicks the live sender
        self.current = ws
        if old is not None and not old.closed:
            await old.close(code=4002, message=b"replaced by a new sender")
        log.info("sender connected (origin=%s)", origin)

        utt = None
        ignored = False                         # current utterance is being ignored (bad format / no session)
        listener = None
        pump = None

        def detach():
            nonlocal listener, pump
            if pump is not None:
                pump.cancel()
            if listener is not None and listener[1] in listener[0].msgqueues:
                listener[0].msgqueues.remove(listener[1])
            listener = pump = None

        def abort_current():
            nonlocal utt
            if utt is not None:
                self._flush(utt, final=True)
                utt = None

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if utt is None or ignored:
                        continue
                    utt.chunks += 1
                    utt.buf += msg.data
                    self._flush(utt)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        kind = data.get("type")
                    except Exception:
                        await self._send(ws, {"type": "error", "code": "bad_message", "message": "unparsable text message"})
                        continue
                    if kind == "utterance.start":
                        if data.get("version", 1) != 1:
                            await ws.close(code=4003, message=b"unsupported version")
                            break
                        abort_current()
                        ignored = True
                        try:
                            uid = str(uuid.UUID(str(data.get("utterance_id"))))
                        except ValueError:
                            await self._send(ws, {"type": "error", "code": "invalid_utterance_id", "message": "utterance_id is not a UUID"})
                            continue
                        if (data.get("format"), data.get("sample_rate"), data.get("channels")) != ("pcm_s16le", 16000, 1):
                            await self._send(ws, {"type": "error", "code": "unsupported_format", "utterance_id": uid,
                                                  "message": "need pcm_s16le / 16000 Hz / 1 channel"})
                            continue
                        sess = self.get_session()
                        if sess is None:
                            await self._send(ws, {"type": "error", "code": "no_session", "utterance_id": uid,
                                                  "message": "no LiveTalking session (open the video page first)"})
                            continue
                        emotion = data.get("emotion") or "neutral"
                        if emotion not in EMOTIONS:
                            log.warning("unknown emotion %r -> neutral", emotion)
                            emotion = "neutral"
                        if listener is not None and listener[0] is not sess:   # session changed: re-attach
                            detach()
                        if listener is None:
                            q = queue.Queue(); sess.add_msgqueue(q); listener = (sess, q)
                            pump = asyncio.ensure_future(self._pump(ws, sess, q))
                        utt = _Utt(uid, emotion, sess)
                        ignored = False
                    elif kind == "utterance.end":
                        if utt is None or data.get("utterance_id") != utt.id:
                            continue
                        if data.get("aborted"):
                            log.info("utt %s aborted by sender (%d frames played so far)", utt.id, utt.frames)
                        self._flush(utt, final=True)
                        if not utt.started:      # empty utterance: nothing will play, so close the loop ourselves
                            await self._send(ws, {"type": "playback.started", "utterance_id": utt.id})
                            await self._send(ws, {"type": "playback.ended", "utterance_id": utt.id})
                        utt = None
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            abort_current()                      # disconnect mid-utterance: close it so the mouth does not hang open
            detach()
            if self.current is ws:
                self.current = None
            log.info("sender disconnected exc=%r code=%s", ws.exception(), ws.close_code)
        return ws

    async def _pump(self, ws, sess, q):
        """LiveTalking announces start/end when it ACTUALLY plays our frames -> playback.* to the sender,
        and the emotion to /sse listeners at the same moment."""
        try:
            while not ws.closed:
                try:
                    m = json.loads(q.get_nowait())
                except queue.Empty:
                    await asyncio.sleep(0.02)
                    continue
                except Exception:
                    continue
                uid = m.get("vendor_utt")
                if not uid:
                    continue
                if m.get("status") == "start":
                    sess.send_msg(json.dumps({"type": "emotion", "utterance_id": uid, "emotion": m.get("emotion", "neutral")}))
                    await self._send(ws, {"type": "playback.started", "utterance_id": uid})
                elif m.get("status") == "end":
                    await self._send(ws, {"type": "playback.ended", "utterance_id": uid})
        except asyncio.CancelledError:
            pass
