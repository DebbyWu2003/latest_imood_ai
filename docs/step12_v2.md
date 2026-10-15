# Step 12 v2: persistent low-latency streaming demo

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
livetalking_cyber_gf.py  (Ubuntu-24.04, .venv-lt; runs the UNMODIFIED LiveTalking app.py)            :8010
  POST /cyber_gf/audio_stream  body = continuous 16 kHz float32-LE PCM
    → 320-sample (20 ms) frames → avatar_session.asr.queue   (one 'start' frame … one trailing 'end' frame)  ④
  Wav2Lip (MelASR → wav2lip256_myavatar) ⑤ → WebRTC player ⑥ → browser
```
- Producer (TTS) and consumer (sender) run concurrently. TTS never waits for playback.
- LiveTalking's queue is unbounded, and the WebRTC player paces playback at real time.

## Streaming transport (Phase A decision)
What was inspected in the installed LiveTalking (commit b3e7490, which is also upstream `main`):

| Item | Finding |
|---|---|
| `BaseAvatar.put_audio_frame(chunk, datainfo)` | forwards to `self.asr.put_audio_frame` → `BaseASR.queue.put(AudioFrameData(data, type=0, userdata=datainfo))` (16 kHz, 20 ms float32) |
| `put_audio_file` / `/humanaudio` | decodes a whole file, resamples it to 16 kHz with resampy, and emits `start` on the first frame and `end` on the last, **per call** |
| `BaseASR.get_audio_frame` | `queue.get(timeout=0.01)`; **empty → silence frame `type=1`** (the mouth closes) |
| `start` / `end` events | travel with the frame and are sent through `/sse` (`notify`) **when the WebRTC audio track plays that frame** |
| Official continuous endpoint | **none** (the `/api/asr` WebSocket is FunASR microphone ASR). Upstream `main` has none either. |
| Official streaming pattern | `tts/qwentts.py` (DashScope cloud TTS): 16 kHz float32 → 320-sample frames → `put_audio_frame`, `start` on the first frame only, a trailing zero frame with `end` |

Chosen approach: **option 2, a project-owned adapter, with no LiveTalking file modified.**
- `scripts/wsl/livetalking_cyber_gf.py` wraps `server.routes.setup_routes` at runtime (it registers project routes first, because upstream ends with a catch-all static `/`), then runs `app.py` through `runpy` with the same arguments.
- Repeated `/humanaudio` POSTs were **not** used: every call has its own start/end, and any gap between calls becomes silence frames.
- Routes added: `POST /cyber_gf/audio_stream`, `GET /cyber_gf/utt/{utt}`, `GET /cyber_gf/stats`, `GET /imood/…` (frontend page). All upstream routes and pages are unchanged; `/humanaudio` still works.

Runtime-only hooks inside the adapter (instance-level, no file edits):
1. **Atomic batch enqueue.** Each received block is enqueued under `asr.queue.mutex`, using the same `AudioFrameData(type=0, userdata=eventpoint)` items that `put_audio_frame()` creates.
   - Why: `MelASR.run_step` pulls 32 frames in a tight loop with a 10 ms `get()` timeout. With per-frame `put_audio_frame` calls, the ASR thread sometimes took the `start` frame and then timed out (GIL contention with the inference and render threads) before the next put. That inserted 2 silence frames at +0.01 s (measured in 4 of 12 runs).
   - Result: after the change, **0 of 5**.
2. **Pre-roll of 24 frames (480 ms)** before the first enqueue. The first TTS chunk (~0.62 s after resampling) reaches the adapter as several TCP reads a few ms apart, and a first batch of only a few frames could still starve MelASR (1 silence frame at +0.01 s, seen once on a cold start). Every frame comes from the first chunk, so no wait is added. Verified with 5 of 5 runs at 0 underruns, including the first run after a cold start (2026-09-29, via the repo launchers).
3. `asr.get_audio_frame` is wrapped on the instance to count silence frames **inside** an utterance (underruns) and their position.
4. `avatar_session.add_msgqueue()` records the actual playback time of our `start`/`end` frames.
5. A logging handler captures LiveTalking's `actual avg infer/final fps` lines.

## LLM → TTS (Phase D): full reply by default
Both modes are implemented (`config/step12_v2.json → llm_to_tts.mode`, overridable per request with `?mode=sentence|full`). The sentence segmenter:
- emits on `。！？!?；;…`;
- merges segments shorter than 8 speakable characters;
- falls back to cutting at the last comma once 60 characters accumulate;
- asserts that `"".join(segments) == reply`.

Measured with alternating runs:

| Mode | Avatar speaking start (s) | Why |
|---|---|---|
| sentence | 3.08, 3.27, 3.29, 3.52 → **mean 3.29** | The first sentence is ready at about 1.1 s, but TTS's first chunk only arrives at about 2.25 s: TTS and llama.cpp share the GPU while the LLM is still generating |
| **full** | 2.94, 2.99, 3.17, 3.20 → **mean 3.07** | The LLM finishes the whole reply in 0.4–0.8 s, and TTS's first chunk follows about 0.39 s later |

**Default: `full`.** It is faster on this machine, simpler, and keeps whole-utterance prosody. Sentence mode would only pay off if the LLM ran on a different GPU or replies were much longer.

## Results (final configuration: 5 consecutive runs, `assets/test.wav`, full mode)
Seconds from request received:

| Stage | Run 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| ASR done | 1.36 | 0.87 | 0.86 | 0.92 | 0.86 |
| LLM first token | 1.46 | 0.95 | 0.94 | 1.00 | 0.93 |
| LLM done (= first text segment) | 1.85 | 1.56 | 1.51 | 1.48 | 1.39 |
| TTS first chunk = first audio sent = first frame in LiveTalking | 2.32 | 1.95 | 1.89 | 1.86 | 1.80 |
| **Avatar speaking start** (WebRTC plays `start`) | **3.35** | **3.21** | **3.14** | **3.10** | **2.95** |
| TTS done | 6.53 | 7.83 | 7.86 | 6.70 | 6.71 |
| Avatar speaking end | 14.07 | 18.25 | 18.66 | 15.74 | 15.03 |
| Audio length / avatar spoke | 10.72 / 10.72 | 15.04 / 15.04 | 15.52 / 15.52 | 12.64 / 12.64 | 12.08 / 12.08 |
| Underruns / overflows | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Max LiveTalking queue (frames of 20 ms) | 321 | 465 | 486 | 378 | 337 |
| LiveTalking final / infer fps | 25.0 / 115 | 25.0 / 108 | 25.0 / 98 | 25.0 / 113 | 25.0 / 109 |
| Viewer video fps | 25.05 | 25.06 | 24.97 | 25.07 | 25.09 |

**Speaking start: mean 3.15 s, vs 6.91 s for the MVP (−54%).** Model loading happens only at service start (Whisper about 4–5 s, TTS + CUDA graphs about 11–16 s, warm-up about 2 s).

Remaining latency budget (run 5):
- ASR 0.86 s
- LLM 0.53 s
- TTS first chunk 0.41 s
- **LiveTalking's internal pipeline 1.15 s** (first frame enqueued → WebRTC plays it: 32-frame MelASR batches, 10-frame right context, Wav2Lip batch, player queue). This is now the largest single term.

### Gaps and sync evidence
For each run, `outputs/step12_v2/<job>/` holds:
- `reply_24k_tts.wav` (raw TTS)
- `reply_16k_sent.wav` (what was streamed)
- `webrtc_received_audio.wav` (what a WebRTC viewer received)
- `livetalking_record.mp4` (LiveTalking's own recorder)

Pause analysis (runs of 20 ms RMS < 0.004 lasting 60 ms or more, inside the answer):
- The pause lists and voiced spans are **identical** between the TTS output, the streamed audio and the recording (within 20 ms). Every pause is a natural pause in the TTS speech; **streaming inserts none**.
- The recording's video (25 fps) and audio durations match within one frame.

## Metrics logged per question
`outputs/step12_v2/<job>/timings.json` (also shown in the imood page's 診斷資訊 → 延遲 tab):
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

## Known issue: stutter in remote live viewing (non-blocking)
- **Symptom:** the saved recording is smooth and in sync, but live viewing over a remote desktop sometimes stutters.
- **Server side is healthy:** LiveTalking final fps is 25.0 and infer fps about 100–115. The headless WebRTC viewer on the same machine receives 25.0–25.1 fps, with max inter-frame intervals of 75–97 ms (a few frames over 60 ms per answer).
- **Likely cause:** the remote-display path (AnyDesk and the second Windows session also hold GPU memory) and/or browser decode or jitter buffering.
- **Diagnostics:** the imood page (診斷資訊 → WebRTC 接收) shows live `getStats()` numbers: decoded fps, frames dropped, freeze count and duration, jitter-buffer delay, audio concealed samples. Compare them locally vs over the remote desktop.

## VRAM
Full system running: **23.3 GB of 24 GB**.
- llama-server about 10.0 GB
- WSL `vmwp` about 9.2 GB (voice service: Whisper + TTS + CUDA graphs, plus LiveTalking)
- the desktop

On the reference machine, two unrelated GPU services (a CosyVoice TTS on :8001 and a service on :8000) must stay stopped while the demo runs.

## Start / stop
- **Start everything:** `scripts\windows\start_step12_v2.bat`. It starts llama-server if it isn't running, starts both WSL services **in Ubuntu-24.04**, waits until they are ready, and opens the imood page.
  - WSL only: `wsl -d Ubuntu-24.04 -- bash <repo-in-wsl>/scripts/wsl/start_step12_v2.sh`
- **Stop:** `scripts\windows\stop_step12_v2.bat` stops **everything** (both WSL services and llama-server) and prints the remaining VRAM (desktop only, about 1.5 GB).
- **Logs:** `/root/lt_logs/lt_cyber_gf.log` (LiveTalking + adapter), `logs/voice_service.log`.
- **Headless test:** `wsl -d Ubuntu-24.04 -- bash -c "source /root/livetalking/LiveTalking/.venv-lt/bin/activate && cd <repo-in-wsl> && python scripts/wsl/perf/step12_v2_e2e_test.py [--mode full|sentence]"`

## Fall back to the Step 12 MVP
1. `scripts\windows\stop_step12_v2.bat` (this also stops llama-server), then start llama again with `scripts\windows\start_llama.bat`. The v2 voice service must be stopped because running both would not fit in 24 GB.
2. Start LiveTalking the MVP way: `wsl -d Ubuntu-24.04 -- bash <repo-in-wsl>/scripts/wsl/lt_test_start.sh`, or keep the v2 launcher alone, since upstream `/humanaudio` is unchanged.
3. Open http://localhost:8010/index.html → connect, then run `scripts\windows\run_step12_mvp.bat`.

## Not done (as instructed)
Microphone capture, VAD, interruption (`/interrupt_talk` exists upstream), and reducing LiveTalking's internal ~1.1 s pipeline latency.
