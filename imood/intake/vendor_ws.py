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


class _Utterance:
    def __init__(self, utterance_id, emotion, session):
        self.id, self.emotion, self.session = utterance_id, emotion, session
        self.pending_audio = b""
        self.started = False       # 'start' frame already enqueued
        self.frames = 0
        self.chunks = 0


class VendorReceiver:
    def __init__(self, get_session, make_item, token=None, origins=None, on_emotion=None):
        self.on_emotion = on_emotion            # (sess, emotion, speaking) -> None; switches the avatar's face, see livetalking_cyber_gf.set_emotion
        self.get_session = get_session          # () -> avatar session or None
        self.make_item = make_item              # (float32 ndarray, userdata dict) -> item for sess.asr.queue
        self.token = token if token is not None else os.environ.get("VENDOR_VOICE_TOKEN", "")
        extra = {o for o in os.environ.get("VENDOR_ALLOWED_ORIGINS", "").split(",") if o}
        self.origins = set(origins) if origins is not None else DEFAULT_ORIGINS | extra
        self.current = None                     # the one active sender websocket
        if not self.token:
            log.warning("VENDOR_VOICE_TOKEN is not set: every connection will be rejected with 4001")

    # ---- helpers -----------------------------------------------------------------------------
    async def _send(self, ws, message):
        if not ws.closed:
            try:
                await ws.send_json(message)
            except ConnectionError:
                pass

    def _enqueue(self, session, frames, first_meta=None, end_meta=None):
        """Put a block of float32 frames on the ASR queue atomically (same reasoning as put_batch in
        livetalking_cyber_gf.py): all frames become visible at once so MelASR's 10 ms get() cannot starve."""
        items = []
        for index, frame in enumerate(frames):
            items.append(self.make_item(frame, dict(first_meta) if (index == 0 and first_meta) else {}))
        if end_meta is not None:
            items.append(self.make_item(np.zeros(FRAME, np.float32), dict(end_meta)))
        asr_queue = session.asr.queue
        with asr_queue.mutex:
            for item in items:
                asr_queue._put(item); asr_queue.unfinished_tasks += 1
            asr_queue.not_empty.notify_all()

    @staticmethod
    def _to_frames(pcm_bytes):
        samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        return [samples[i * FRAME:(i + 1) * FRAME].copy() for i in range(len(samples) // FRAME)]

    def _meta(self, utterance, status):
        return {"status": status, "vendor_utt": utterance.id, "emotion": utterance.emotion}

    def _flush(self, utterance, final=False):
        """Release buffered audio: first after PREROLL_FRAMES, then in BLOCK_FRAMES blocks; everything on final."""
        frame_count = len(utterance.pending_audio) // FRAME_BYTES
        if not final and frame_count < (BLOCK_FRAMES if utterance.started else PREROLL_FRAMES):
            return
        frames = self._to_frames(utterance.pending_audio[:frame_count * FRAME_BYTES])
        leftover = utterance.pending_audio[frame_count * FRAME_BYTES:]
        utterance.pending_audio = b""
        if final and len(leftover) >= 2:                           # zero-pad the last partial frame
            padded_frame = np.zeros(FRAME, np.float32)
            tail_samples = np.frombuffer(leftover[:len(leftover) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            padded_frame[:len(tail_samples)] = tail_samples
            frames.append(padded_frame)
        elif not final:
            utterance.pending_audio = leftover
        if not frames:
            return
        first_meta = None if utterance.started else self._meta(utterance, "start")
        utterance.started = True
        self._enqueue(utterance.session, frames, first_meta=first_meta, end_meta=self._meta(utterance, "end") if final else None)
        utterance.frames += len(frames)

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

        previous_sender = self.current         # only after auth, so a failed attempt never kicks the live sender
        self.current = ws
        if previous_sender is not None and not previous_sender.closed:
            await previous_sender.close(code=4002, message=b"replaced by a new sender")
        log.info("sender connected (origin=%s)", origin)

        utterance = None
        ignored = False                         # current utterance is being ignored (bad format / no session)
        listener = None
        pump_task = None

        def detach():
            nonlocal listener, pump_task
            if pump_task is not None:
                pump_task.cancel()
            if listener is not None and listener[1] in listener[0].msgqueues:
                listener[0].msgqueues.remove(listener[1])
            listener = pump_task = None

        def abort_current():
            nonlocal utterance
            if utterance is not None:
                self._flush(utterance, final=True)
                utterance = None

        try:
            async for ws_message in ws:
                if ws_message.type == WSMsgType.BINARY:
                    if utterance is None or ignored:
                        continue
                    utterance.chunks += 1
                    utterance.pending_audio += ws_message.data
                    self._flush(utterance)
                elif ws_message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(ws_message.data)
                        message_type = payload.get("type")
                    except Exception:
                        await self._send(ws, {"type": "error", "code": "bad_message", "message": "unparsable text message"})
                        continue
                    if message_type == "utterance.start":
                        if payload.get("version", 1) != 1:
                            await ws.close(code=4003, message=b"unsupported version")
                            break
                        abort_current()
                        ignored = True
                        try:
                            utterance_id = str(uuid.UUID(str(payload.get("utterance_id"))))
                        except ValueError:
                            await self._send(ws, {"type": "error", "code": "invalid_utterance_id", "message": "utterance_id is not a UUID"})
                            continue
                        if (payload.get("format"), payload.get("sample_rate"), payload.get("channels")) != ("pcm_s16le", 16000, 1):
                            await self._send(ws, {"type": "error", "code": "unsupported_format", "utterance_id": utterance_id,
                                                  "message": "need pcm_s16le / 16000 Hz / 1 channel"})
                            continue
                        session = self.get_session()
                        if session is None:
                            await self._send(ws, {"type": "error", "code": "no_session", "utterance_id": utterance_id,
                                                  "message": "no LiveTalking session (open the video page first)"})
                            continue
                        emotion = payload.get("emotion") or "neutral"
                        if emotion not in EMOTIONS:
                            log.warning("unknown emotion %r -> neutral", emotion)
                            emotion = "neutral"
                        if listener is not None and listener[0] is not session:   # session changed: re-attach
                            detach()
                        if listener is None:
                            message_queue = queue.Queue(); session.add_msgqueue(message_queue); listener = (session, message_queue)
                            pump_task = asyncio.ensure_future(self._pump(ws, session, message_queue))
                        utterance = _Utterance(utterance_id, emotion, session)
                        if not getattr(session, "speaking", False):     # idle: change the face now so it is ready (and faded) when the voice starts
                            self._emotion(session, emotion, speaking=True)
                        ignored = False
                    elif message_type == "utterance.end":
                        if utterance is None or payload.get("utterance_id") != utterance.id:
                            continue
                        if payload.get("aborted"):
                            log.info("utt %s aborted by sender (%d frames played so far)", utterance.id, utterance.frames)
                        self._flush(utterance, final=True)
                        if not utterance.started:      # empty utterance: nothing will play, so close the loop ourselves
                            await self._send(ws, {"type": "playback.started", "utterance_id": utterance.id})
                            await self._send(ws, {"type": "playback.ended", "utterance_id": utterance.id})
                        utterance = None
                elif ws_message.type == WSMsgType.ERROR:
                    break
        finally:
            abort_current()                      # disconnect mid-utterance: close it so the mouth does not hang open
            detach()
            if self.current is ws:
                self.current = None
            log.info("sender disconnected exc=%r code=%s", ws.exception(), ws.close_code)
        return ws

    def _emotion(self, session, emotion, speaking=False):
        if self.on_emotion:
            try:
                self.on_emotion(session, emotion, speaking)
            except Exception:
                log.exception("on_emotion(%r) failed", emotion)

    async def _pump(self, ws, session, message_queue):
        """LiveTalking announces start/end when it ACTUALLY plays the project's frames -> playback.* to the sender,
        and the emotion to /sse listeners at the same moment."""
        last_emotion = {}
        try:
            while not ws.closed:
                try:
                    event = json.loads(message_queue.get_nowait())
                except queue.Empty:
                    await asyncio.sleep(0.02)
                    continue
                except Exception:
                    continue
                utterance_id = event.get("vendor_utt")
                if not utterance_id:
                    continue
                if event.get("status") == "start":
                    last_emotion[utterance_id] = event.get("emotion", "neutral")
                    self._emotion(session, last_emotion[utterance_id], speaking=True)
                    session.send_msg(json.dumps({"type": "emotion", "utterance_id": utterance_id, "emotion": event.get("emotion", "neutral")}))
                    await self._send(ws, {"type": "playback.started", "utterance_id": utterance_id})
                elif event.get("status") == "end":
                    # keep the utterance's emotion on its idle face after speech
                    self._emotion(session, last_emotion.pop(utterance_id, "neutral"))
                    await self._send(ws, {"type": "playback.ended", "utterance_id": utterance_id})
        except asyncio.CancelledError:
            pass
