"""Microphone intake for full mode: WebSocket /ws/audio (PCM16 16 kHz mono chunks) -> VAD -> ASR -> coalesce -> Job.

Replaces the per-utterance wav upload when the page streams the microphone. Messages back (JSON, "type"):
ack, asr_start, asr_empty, transcript, job (id of the queued answer job; its progress is on GET /api/job/<id>), error.
Query: session=<LiveTalking sessionid> (empty -> the newest session is found automatically).
"""
import asyncio, io, threading, time, wave

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from imood.common import config as cfgmod
from imood.intake import transcriber
from imood.intake.coalescer import Coalescer
from imood.intake.mic_endpointer import EndpointConfig, Endpointer, SAMPLE_RATE

app = FastAPI()
_ASR_LOCK = threading.Lock()
_ENV = {}        # filled by start(): submit(text, wav, sessionid) -> job id; asr_model; asr_cfg


def _wav(pcm):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE); w.writeframes(pcm)
    return b.getvalue()


def _asr(pcm):
    with _ASR_LOCK:
        return transcriber.transcribe(_ENV["asr_model"], _wav(pcm), _ENV["asr_cfg"])[0]


@app.websocket("/ws/audio")
async def audio(ws: WebSocket):
    await ws.accept()
    vad = cfgmod.load().get("vad", {})
    fields = {k: v for k, v in vad.items() if k in EndpointConfig.__dataclass_fields__}
    endpointer = Endpointer(EndpointConfig(**fields))
    co = Coalescer(vad.get("coalesce_ms", 1000), vad.get("coalesce_max_segments", 6))
    session = ws.query_params.get("session", "")
    loop = asyncio.get_running_loop()
    chunks, pcm_of_pending = 0, bytearray()

    async def send_job(text):
        wav = _wav(bytes(pcm_of_pending)); pcm_of_pending.clear()
        job_id = _ENV["submit"](text, wav, session)
        await ws.send_json({"type": "job", "job": job_id, "text": text})

    try:
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_bytes(), timeout=0.25 if co.pending else None)
            except asyncio.TimeoutError:
                data = None
            if data is not None:
                if len(data) % 2:
                    await ws.send_json({"type": "error", "error": f"chunk length {len(data)} is not a multiple of 2 (PCM16)"})
                else:
                    chunks += 1
                    await ws.send_json({"type": "ack", "ack": chunks, "chunk_ms": round(len(data) / 2 / SAMPLE_RATE * 1000, 1)})
                    utt = endpointer.feed(data)
                    if utt is not None:
                        await ws.send_json({"type": "asr_start", "audio_ms": round(utt.duration_ms)})
                        t0 = time.perf_counter()
                        text = await loop.run_in_executor(None, _asr, utt.pcm)
                        if not text:
                            await ws.send_json({"type": "asr_empty"})
                        else:
                            await ws.send_json({"type": "transcript", "text": text, "audio_ms": round(utt.duration_ms),
                                                "asr_latency_ms": round((time.perf_counter() - t0) * 1000)})
                            pcm_of_pending.extend(utt.pcm)
                            out = co.add(text)
                            if out:
                                await send_job(out)
            out = co.due(endpointer.triggered, endpointer.silence_since_last_ms)
            if out:
                await send_job(out)
    except WebSocketDisconnect:
        pass


def start(submit, asr_model, asr_cfg):
    """Run the microphone server in a daemon thread (uvicorn) on intake.mic.port; no-op when disabled."""
    c = cfgmod.load().get("intake", {}).get("mic", {})
    if not c.get("enabled", True):
        return None
    import uvicorn
    _ENV.update(submit=submit, asr_model=asr_model, asr_cfg=asr_cfg)
    server = uvicorn.Server(uvicorn.Config(app, host=c.get("host", "127.0.0.1"), port=int(c.get("port", 8021)), log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server
