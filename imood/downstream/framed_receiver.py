"""Framed TCP receiver, runs inside the LiveTalking process (started from livetalking_plugin).

Adapter: one utterance (BEGIN, AUDIO*, END) is fed into the existing continuous-audio ingest
(`audio_stream`) through a request-shaped object, so frame splitting, pre-roll, queueing and the per-utterance
stats stay exactly the ones the HTTP path uses. BEGIN.emotion (vendor string) switches the face first.
STATUS goes back: received -> playback_started -> playback_ended (or error).
"""
import asyncio, json, time

import numpy as np

from imood.common import framing


class _ChunkStream:
    def __init__(self, chunk_queue):
        self.chunk_queue = chunk_queue

    async def iter_any(self):
        while True:
            chunk = await self.chunk_queue.get()
            if chunk is None:
                return
            yield chunk


class _IngestRequest:
    def __init__(self, query, chunk_queue):
        self.query = query
        self.content = _ChunkStream(chunk_queue)


async def _read_frame(reader):
    frame_type, payload_length = framing.unpack_header(await reader.readexactly(framing.HEADER.size))
    return frame_type, (await reader.readexactly(payload_length) if payload_length else b"")


async def _send_status(writer, utterance_id, state, **extra):
    payload = json.dumps({"utterance": utterance_id, "state": state, **extra}).encode("utf-8")
    writer.write(framing.pack(framing.STATUS, payload))
    await writer.drain()


def make_handler(ingest, find_session, apply_emotion, utterance_stats):
    """ingest(request) -> response (the audio_stream coroutine); find_session(id or "") -> (sessionid, session) or (None, None);
    apply_emotion(session, vendor_string); utterance_stats(utterance_id) -> stats dict or None."""

    async def handle(reader, writer):
        task, audio_queue, utterance_id = None, None, ""
        try:
            while True:
                frame_type, payload = await _read_frame(reader)
                if frame_type == framing.BEGIN:
                    meta = json.loads(payload.decode("utf-8"))
                    utterance_id = meta.get("utterance") or f"u{int(time.time() * 1000)}"
                    session_id, session = find_session(meta.get("session", ""))
                    if session is None:
                        await _send_status(writer, utterance_id, "error", msg="session not found")
                        return
                    if meta.get("emotion"):
                        apply_emotion(session, meta["emotion"])
                    audio_queue = asyncio.Queue()
                    task = asyncio.ensure_future(ingest(_IngestRequest({"sessionid": session_id, "utt": utterance_id}, audio_queue)))
                elif frame_type == framing.AUDIO and audio_queue is not None:
                    audio_queue.put_nowait((np.frombuffer(payload, dtype="<i2").astype("<f4") / 32768.0).tobytes())
                elif frame_type == framing.END and audio_queue is not None:
                    audio_queue.put_nowait(None)
                    await task
                    await _send_status(writer, utterance_id, "received")
                    await _follow_playback(writer, utterance_id, utterance_stats)
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as error:
            try:
                await _send_status(writer, utterance_id, "error", msg=str(error))
            except Exception:
                pass
        finally:
            if task is not None and not task.done():
                task.cancel()
            writer.close()

    return handle


async def _follow_playback(writer, utterance_id, utterance_stats, timeout=180):
    sent_start, deadline = False, time.time() + timeout
    while time.time() < deadline:
        stats = utterance_stats(utterance_id) or {}
        if stats.get("playback_start") and not sent_start:
            sent_start = True
            await _send_status(writer, utterance_id, "playback_started")
        if stats.get("playback_end"):
            await _send_status(writer, utterance_id, "playback_ended")
            return
        await asyncio.sleep(0.05)
    await _send_status(writer, utterance_id, "error", msg="playback timeout")
