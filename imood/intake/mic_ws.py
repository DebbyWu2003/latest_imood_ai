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
_SERVICE_STATE = {}        # filled by start(): submit(text, wav, sessionid) -> job id; asr_model; asr_config


def _pcm_to_wav(pcm):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1); wav_file.setsampwidth(2); wav_file.setframerate(SAMPLE_RATE); wav_file.writeframes(pcm)
    return buffer.getvalue()


def _recognize(pcm):
    with _ASR_LOCK:
        return transcriber.transcribe(_SERVICE_STATE["asr_model"], _pcm_to_wav(pcm), _SERVICE_STATE["asr_config"])[0]


@app.websocket("/ws/audio")
async def microphone_stream(ws: WebSocket):
    await ws.accept()
    vad_config = cfgmod.load().get("vad", {})
    endpoint_fields = {key: value for key, value in vad_config.items() if key in EndpointConfig.__dataclass_fields__}
    endpointer = Endpointer(EndpointConfig(**endpoint_fields))
    coalescer = Coalescer(vad_config.get("coalesce_ms", 1000), vad_config.get("coalesce_max_segments", 6))
    session = ws.query_params.get("session", "")
    loop = asyncio.get_running_loop()
    chunk_count, pending_pcm = 0, bytearray()

    async def send_job(text):
        wav = _pcm_to_wav(bytes(pending_pcm)); pending_pcm.clear()
        job_id = _SERVICE_STATE["submit"](text, wav, session)
        await ws.send_json({"type": "job", "job": job_id, "text": text})

    try:
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_bytes(), timeout=0.25 if coalescer.pending else None)
            except asyncio.TimeoutError:
                data = None
            if data is not None:
                if len(data) % 2:
                    await ws.send_json({"type": "error", "error": f"chunk length {len(data)} is not a multiple of 2 (PCM16)"})
                else:
                    chunk_count += 1
                    await ws.send_json({"type": "ack", "ack": chunk_count, "chunk_ms": round(len(data) / 2 / SAMPLE_RATE * 1000, 1)})
                    utterance = endpointer.feed(data)
                    if utterance is not None:
                        await ws.send_json({"type": "asr_start", "audio_ms": round(utterance.duration_ms)})
                        started = time.perf_counter()
                        text = await loop.run_in_executor(None, _recognize, utterance.pcm)
                        if not text:
                            await ws.send_json({"type": "asr_empty"})
                        else:
                            await ws.send_json({"type": "transcript", "text": text, "audio_ms": round(utterance.duration_ms),
                                                "asr_latency_ms": round((time.perf_counter() - started) * 1000)})
                            pending_pcm.extend(utterance.pcm)
                            merged_text = coalescer.add(text)
                            if merged_text:
                                await send_job(merged_text)
            merged_text = coalescer.due(endpointer.triggered, endpointer.silence_since_last_ms)
            if merged_text:
                await send_job(merged_text)
    except WebSocketDisconnect:
        pass


def start(submit, asr_model, asr_config):
    """Run the microphone server in a daemon thread (uvicorn) on intake.mic.port; no-op when disabled."""
    mic_config = cfgmod.load().get("intake", {}).get("mic", {})
    if not mic_config.get("enabled", True):
        return None
    import uvicorn
    _SERVICE_STATE.update(submit=submit, asr_model=asr_model, asr_config=asr_config)
    server = uvicorn.Server(uvicorn.Config(app, host=mic_config.get("host", "127.0.0.1"), port=int(mic_config.get("port", 8021)), log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server
