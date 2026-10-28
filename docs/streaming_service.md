# Streaming service

Persistent voice service with streamed audio into the LiveTalking avatar. Models are loaded once at service start; each question pays only inference time.

## Architecture
```
Browser  http://localhost:8010/imood/imood_ai.html   (project page, served by LiveTalking :8010)
  │ ① WebRTC connect (POST /offer → sessionid)             ◄──────────── ⑦ avatar video + audio (WebRTC)
  │ ② question.wav  POST http://localhost:8020/api/ask?sessionid=…   (poll /api/job/<id> for state + metrics)
  ▼
voice_service.py  (Ubuntu-24.04, .venv-tts-fast, models loaded ONCE)                                   :8020
  ASR  Faster-Whisper large-v3 (fp16)
  LLM  llama.cpp Qwen3-14B  http://localhost:8090  (streamed; persona from config/persona.txt)
  TTS  FasterQwen3TTS.generate_voice_clone_streaming(chunk_size=8)   [producer thread]
         ref.wav + ref_text.txt, ICL, append_silence=False, bf16/sdpa, CUDA graphs kept hot
       → soxr.ResampleStream 24 kHz → 16 kHz (ONE stateful resampler per utterance, float32 mono)
       → in-memory queue
  sender thread → ONE chunked HTTP POST per utterance  ③
  ▼
livetalking_cyber_gf.py  (Ubuntu-24.04, .venv-lt; runs the unmodified LiveTalking app.py)            :8010
  POST /cyber_gf/audio_stream  body = continuous 16 kHz float32-LE PCM
    → 320-sample (20 ms) frames → avatar_session.asr.queue   (one 'start' frame … one trailing 'end' frame)  ④
  Wav2Lip (MelASR → wav2lip256 avatar) ⑤ → WebRTC player ⑥ → browser
```
- Producer (TTS) and consumer (sender) run concurrently. TTS never waits for playback.
- LiveTalking's queue is unbounded, and the WebRTC player paces playback at real time.
- The `full` mode of `imood.launcher` sends the same audio over the framed TCP link instead of the HTTP POST; see [modes.md](modes.md).

## Streaming transport
How LiveTalking consumes audio:

| Item | Behaviour |
|---|---|
| `BaseAvatar.put_audio_frame(chunk, datainfo)` | forwards to `BaseASR.queue.put(AudioFrameData(data, type=0, userdata=datainfo))` (16 kHz, 20 ms float32) |
| `put_audio_file` / `/humanaudio` | decodes a whole file, resamples it to 16 kHz, and emits `start` on the first frame and `end` on the last, per call |
| `BaseASR.get_audio_frame` | `queue.get(timeout=0.01)`; an empty queue yields a silence frame (`type=1`), so the mouth closes |
| `start` / `end` events | travel with the frame and are sent through `/sse` (`notify`) when the WebRTC audio track plays that frame |
| Continuous-audio endpoint | none upstream |

The project adds one with an adapter that modifies no LiveTalking file:
- `scripts/wsl/livetalking_cyber_gf.py` wraps `server.routes.setup_routes` at runtime (it registers the project routes first, because upstream ends with a catch-all static `/`), then runs `app.py` through `runpy` with the same arguments.
- Routes added: `POST /cyber_gf/audio_stream`, `GET /cyber_gf/utt/{utt}`, `GET /cyber_gf/stats`, `GET /imood/…` (frontend page). All upstream routes and pages are unchanged; `/humanaudio` still works.
- The stream follows the pattern of LiveTalking's own streaming TTS integration (`tts/qwentts.py`): 16 kHz float32 → 320-sample frames → `put_audio_frame`, `start` on the first frame only, a trailing zero frame with `end`.

Runtime-only hooks inside the adapter (instance-level, no file edits):
1. **Atomic batch enqueue.** Each received block is enqueued under `asr.queue.mutex`, using the same `AudioFrameData(type=0, userdata=eventpoint)` items that `put_audio_frame()` creates. `MelASR.run_step` pulls 32 frames in a tight loop with a 10 ms `get()` timeout; with per-frame puts the ASR thread could take the `start` frame and then time out before the next put, which inserts silence frames.
2. **Pre-roll of 24 frames (480 ms)** before the first enqueue. The first TTS chunk reaches the adapter as several TCP reads a few milliseconds apart, and a first batch of only a few frames could still starve MelASR. All frames come from the first chunk, so no wait is added.
3. `asr.get_audio_frame` is wrapped on the instance to count silence frames inside an utterance (underruns) and their position.
4. `avatar_session.add_msgqueue()` records the actual playback time of the project's `start`/`end` frames.
5. A logging handler captures LiveTalking's `actual avg infer/final fps` lines.

## LLM → TTS
Two modes (`config/streaming_service.json → llm_to_tts.mode`, overridable per request with `?mode=sentence|full`):
- `full` (default): TTS starts when the whole LLM reply is complete, which keeps whole-utterance prosody.
- `sentence`: every finished sentence is handed to TTS at once.

The sentence segmenter:
- emits on `。！？!?；;…`;
- merges segments shorter than 8 speakable characters;
- falls back to cutting at the last comma once 60 characters accumulate;
- asserts that `"".join(segments) == reply`.

## Output files per question
`outputs/step12_v2/<job>/` holds:
- `question.wav`, `transcript.txt`, `reply.txt`
- `reply_24k_tts.wav` (raw TTS)
- `reply_16k_sent.wav` (what was streamed)
- `timings.json` (see below)
- with the headless test: `webrtc_received_audio.wav` (what a WebRTC viewer received), `livetalking_record.mp4` (LiveTalking's own recorder) and `e2e_check.json`

## Metrics logged per question
`timings.json` (also shown in the imood page's 診斷資訊 → 延遲 tab):
- **Timestamps:**
  - `request_received`, `asr_done`, `llm_first_token`
  - `first_segment_ready`, `tts_first_chunk`
  - `first_audio_submitted`, `first_frame_put_livetalking`
  - `avatar_speaking_start`, `llm_done`, `tts_done`
  - `audio_stream_closed`, `avatar_speaking_end`
- **Audio and queue metrics:**
  - `tts_chunks`, `tts_segments`
  - `audio_s_tts_24k`, `audio_s_sent_16k`
  - `lt_frames`, `lt_max_queue_frames`
  - `lt_underruns`, `lt_underrun_frames`, `underrun_at_s`
  - `lt_overflows` (queue longer than 60 s)
  - `lt_fps {infer, final}`, `service_audio_queue_max`

## Diagnostics
The imood page (診斷資訊 → WebRTC 接收) shows live `getStats()` numbers: decoded fps, frames dropped, freeze count and duration, jitter-buffer delay, audio concealed samples.

## VRAM
The full system needs about 23 GB of a 24 GB GPU:
- llama-server about 10 GB
- the WSL side (voice service: Whisper + TTS + CUDA graphs, plus LiveTalking) about 9 GB
- the desktop

Stop other GPU-heavy services before starting; when VRAM is short, TTS runs slower than real time and the audio breaks up.

## Start / stop
- **Start everything:** `scripts\windows\start_avatar_services.bat`. It starts llama-server if it isn't running (with `LEGACY_VOICE=1`), starts the WSL services **in Ubuntu-24.04**, waits until they are ready, and opens the imood page.
  - WSL only: `wsl -d Ubuntu-24.04 -- bash <repo-in-wsl>/scripts/wsl/start_avatar_services.sh`
- **Stop:** `scripts\windows\stop_avatar_services.bat` stops everything (the WSL services and llama-server) and prints the remaining VRAM.
- **Logs:** `/root/lt_logs/lt_cyber_gf.log` (LiveTalking + adapter), `logs/voice_service.log`.
- **Headless test:** `wsl -d Ubuntu-24.04 -- bash -c "source /root/livetalking/LiveTalking/.venv-lt/bin/activate && cd <repo-in-wsl> && python scripts/wsl/perf/streaming_e2e_test.py [--mode full|sentence]"`

## Batch reply
`scripts\windows\run_batch_reply.bat` synthesizes the complete `reply.wav` and posts it to upstream `/humanaudio` (no streaming). Stop the streaming voice service first, because running both does not fit in 24 GB; LiveTalking can be started alone with `scripts/wsl/lt_test_start.sh`. Open http://localhost:8010/index.html, connect, then run the script.
