# Architecture

Details of the streaming service are in [streaming_service.md](streaming_service.md); the two run modes are in [modes.md](modes.md).

## Runtime (per question)
```
 Browser (Windows)  http://localhost:8010/imood/imood_ai.html
   │  WebRTC connect: POST /offer → sessionid                    ▲ avatar video + audio (WebRTC, 25 fps)
   │  question WAV:   POST http://localhost:8020/api/ask         │
   ▼                                                              │
 ┌───────────────────────── WSL Ubuntu-24.04 ──────────────────────────────────────────────────┐
 │ voice_service.py  :8020   (.venv-tts-fast, models loaded once at startup)                    │
 │   Faster-Whisper large-v3 ──► llama.cpp Qwen3-14B (Windows :8090, via localhost) ──►         │
 │   faster-qwen3-tts generate_voice_clone_streaming(chunk_size=8)   ref.wav + ref_text.txt     │
 │   └► soxr 24 kHz→16 kHz (stateful) ──► queue ──► ONE chunked HTTP POST per answer ─┐         │
 │                                                                                    ▼         │
 │ livetalking_cyber_gf.py  :8010  (.venv-lt) = UNMODIFIED LiveTalking app.py + project routes  │
 │   POST /cyber_gf/audio_stream → 20 ms float32 frames → avatar_session.asr.queue              │
 │   MelASR → Wav2Lip (wav2lip256_myavatar) → WebRTC player ─────────────────────────────────────┘
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
 Windows: llama-server.exe (llama.cpp, Qwen3-14B Q4_K_M, :8090)
```

| Component | Process / env | Port |
|---|---|---|
| LLM | `llama-server.exe` (Windows) | 8090 |
| ASR + TTS + orchestration | `scripts/wsl/voice_service.py` (WSL, `.venv-tts-fast`) | 8020 |
| Lip sync + WebRTC + imood page | `scripts/wsl/livetalking_cyber_gf.py` → LiveTalking `app.py` (WSL, `.venv-lt`) | 8010 |

WSL uses mirrored networking (`config/wslconfig.example`), so every component reaches the others via `localhost`.

## One-time asset preparation
```
avatar image (assets/avatar/xiaoya_avatar.webp)
  → ComfyUI + Wan2.2 14B image-to-video (scripts/windows/wan_idle.py, 704×896, 5 s, closed mouth)
  → assets/avatar/idle.mp4
  → LiveTalking avatar preprocessing (scripts/wsl/lt_build_avatar.sh → /api/avatar/task)
  → <LiveTalking>/data/avatars/wav2lip256_myavatar

reference voice: assets/ref.wav (6 s, 24 kHz mono) + config/ref_text.txt (its exact transcript)
  → Qwen3-TTS-12Hz-1.7B-Base ICL voice cloning (prompt built once at voice-service startup)
```

## Alternative flows
- **Batch reply** (non-streaming): `scripts/windows/run_batch_reply.bat` synthesizes the full `reply.wav` and POSTs it to upstream `/humanaudio`.
- **Voice pipeline benchmark** (`qwen-tts` 0.1.1, `.venv-voice`): `scripts/wsl/run_voice_pipeline_bench.sh`.
