"""Framed TCP receiver, runs inside the LiveTalking process (started from livetalking_plugin).

Adapter: one utterance (BEGIN, AUDIO*, END) is fed into the existing continuous-audio ingest
(`audio_stream`) through a request-shaped object, so frame splitting, pre-roll, queueing and the per-utterance
stats stay exactly the ones the HTTP path uses. BEGIN.emotion (vendor string) switches the face first.
STATUS goes back: received -> playback_started -> playback_ended (or error).
"""
import asyncio, json, time

import numpy as np

from imood.common import framing


class _Content:
    def __init__(self, q):
        self.q = q

    async def iter_any(self):
        while True:
            b = await self.q.get()
            if b is None:
                return
            yield b


class _Request:
    def __init__(self, query, q):
        self.query = query
        self.content = _Content(q)


async def _read_frame(reader):
    ftype, n = framing.unpack_header(await reader.readexactly(framing.HEADER.size))
    return ftype, (await reader.readexactly(n) if n else b"")


async def _send_status(writer, utt, state, **extra):
    payload = json.dumps({"utterance": utt, "state": state, **extra}).encode("utf-8")
    writer.write(framing.pack(framing.STATUS, payload))
    await writer.drain()


def make_handler(ingest, find_session, apply_emotion, utt_stats):
    """ingest(request) -> response (the audio_stream coroutine); find_session(id or "") -> (sessionid, session) or (None, None);
    apply_emotion(session, vendor_string); utt_stats(utt) -> stats dict or None."""

    async def handle(reader, writer):
        task, q, utt = None, None, ""
        try:
            while True:
                ftype, payload = await _read_frame(reader)
                if ftype == framing.BEGIN:
                    meta = json.loads(payload.decode("utf-8"))
                    utt = meta.get("utterance") or f"u{int(time.time() * 1000)}"
                    sid, sess = find_session(meta.get("session", ""))
                    if sess is None:
                        await _send_status(writer, utt, "error", msg="session not found")
                        return
                    if meta.get("emotion"):
                        apply_emotion(sess, meta["emotion"])
                    q = asyncio.Queue()
                    task = asyncio.ensure_future(ingest(_Request({"sessionid": sid, "utt": utt}, q)))
                elif ftype == framing.AUDIO and q is not None:
                    q.put_nowait((np.frombuffer(payload, dtype="<i2").astype("<f4") / 32768.0).tobytes())
                elif ftype == framing.END and q is not None:
                    q.put_nowait(None)
                    await task
                    await _send_status(writer, utt, "received")
                    await _follow_playback(writer, utt, utt_stats)
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:
            try:
                await _send_status(writer, utt, "error", msg=str(e))
            except Exception:
                pass
        finally:
            if task is not None and not task.done():
                task.cancel()
            writer.close()

    return handle


async def _follow_playback(writer, utt, utt_stats, timeout=180):
    sent_start, deadline = False, time.time() + timeout
    while time.time() < deadline:
        st = utt_stats(utt) or {}
        if st.get("playback_start") and not sent_start:
            sent_start = True
            await _send_status(writer, utt, "playback_started")
        if st.get("playback_end"):
            await _send_status(writer, utt, "playback_ended")
            return
        await asyncio.sleep(0.05)
    await _send_status(writer, utt, "error", msg="playback timeout")
