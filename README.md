# cyber_gf: local real-time AI avatar

A fully local voice-to-avatar pipeline. A spoken question (WAV) is transcribed by **Faster-Whisper**, answered by **Qwen3-14B** (llama.cpp), and spoken back in a **cloned voice** (Qwen3-TTS via faster-qwen3-tts, streamed). A **LiveTalking / Wav2Lip** avatar lip-syncs the answer live in the browser over **WebRTC**.

> **Speech source (current default): vendor real-time voice.** The local ASR + LLM + TTS pipeline described below is **switched off by default** (`LEGACY_VOICE=0` in `config/paths.env` / `config/paths.bat`); startup then only launches LiveTalking. The avatar speaks audio pushed by the vendor to `ws://localhost:8010/cyber_gf/voice` (needs `VENDOR_VOICE_TOKEN`), see [docs/vendor_voice_receiver.md](docs/vendor_voice_receiver.md) and [docs/emotion_interface.md](docs/emotion_interface.md). The record-a-question button of the imood page only works with `LEGACY_VOICE=1`; sending recordings to the vendor is out of this project's scope (the vendor takes over that part). Everything below describes the legacy pipeline and still works with `LEGACY_VOICE=1`.

**Legacy pipeline status: working end-to-end demo.** Local ASR · local LLM · local cloned TTS · streaming audio · LiveTalking/Wav2Lip avatar · WebRTC browser demo. No cloud APIs are used at runtime.

---

## Quick Start (reference machine, legacy pipeline: set `LEGACY_VOICE=1` first, e.g. in `config/paths.local.env` and `config/paths.local.bat`)

**Prerequisites.** These already exist on the reference machine; on a new machine see [Installation](#installation--reproduction).
- Windows with WSL distro **Ubuntu-24.04**, using mirrored networking (`config/wslconfig.example`)
- llama.cpp + `Qwen3-14B-Q4_K_M.gguf` (default `E:\llama.cpp\`)
- In Ubuntu-24.04:
  - LiveTalking with the `wav2lip256_myavatar` avatar (`/root/livetalking/LiveTalking`)
  - the voice env `/root/cyber_gf/.venv-tts-fast`, plus the Whisper and Qwen3-TTS models in the HF cache
- No other GPU-heavy service running. The stack needs about 23.3 GB of the 24 GB VRAM.

**Run the demo**
1. Start everything:
   ```bat
   scripts\windows\start_step12_v2.bat
   ```
   It starts llama-server in its own window (if it isn't already running), starts the LiveTalking adapter and the voice service in Ubuntu-24.04, and opens the imood page.
2. Wait until the console prints `LiveTalking+adapter: OK` and `voice service: OK`. **The first start loads all models and takes roughly 20–30 s.** Models then stay resident, so later questions don't reload anything.
3. Open (if it didn't open automatically): **http://localhost:8010/imood/imood_ai.html**
4. Pick a character and click **開始視訊** (start video). The avatar video appears within a few seconds.
5. Click the microphone and speak, or choose **`assets/test.wav`** via **上傳 WAV**.
6. For a WAV file, click **送出檔案** (send file); a recording is sent when you click the microphone again.
7. The chat shows the state (asr → thinking → synthesizing → speaking → done), the transcript and the LLM reply; latency metrics are under 診斷資訊 → 延遲. The avatar speaks the reply in the cloned voice.
8. Stop everything (both WSL services and llama-server):
   ```bat
   scripts\windows\stop_step12_v2.bat
   ```

## Expected demo result
With `assets/test.wav`:
- **ASR transcript:** 你今天过得怎么样?有没有什么有趣的事情?
- **LLM reply** (varies every run, persona from `config/persona.txt`), e.g. 今天还不错呀，下午跟闺蜜逛街买了件新衣服，挺好看的。倒是你呢，今天有干什么有趣的事吗？
- **Avatar:** speaks the reply in the voice cloned from `assets/ref.wav`, lip-synced, at 25 fps
- **Time from request to avatar speech:** about **3 s**. Measured mean **3.15 s** over 5 runs on the reference machine (RTX 4090, WSL2). This is a current-machine measurement, not a universal benchmark.

---

## Architecture
```
user WAV (browser upload)
 → Faster-Whisper large-v3                  (WSL, voice service :8020)
 → Qwen3-14B via llama.cpp                  (Windows, :8090)
 → faster-qwen3-tts streaming, chunk_size=8  (cloned voice; soxr 24→16 kHz)
 → streaming adapter                        (project-owned, inside the LiveTalking process, :8010)
 → LiveTalking / Wav2Lip                    (avatar wav2lip256_myavatar)
 → WebRTC
 → browser (imood page)
```
**One-time asset preparation**
```
avatar image → ComfyUI + Wan2.2 image-to-video → assets/avatar/idle.mp4 → LiveTalking avatar preprocessing (wav2lip256_myavatar)
assets/ref.wav + config/ref_text.txt → Qwen3-TTS-12Hz-1.7B-Base voice cloning (ICL prompt, built once at service start)
```
Details: [docs/architecture.md](docs/architecture.md), [docs/step12_v2.md](docs/step12_v2.md).

## Requirements / external dependencies
**In this repo:** integration code, launchers, configs, the imood frontend page, docs, and small assets (reference voice, test question, avatar image, `idle.mp4`).
**Not in this repo** (installed separately; paths are configurable, see [Configuration](#configuration)):

| Component | Used here | Default location |
|---|---|---|
| OS | Windows 11 + WSL2 **Ubuntu-24.04** (mirrored networking) | — |
| GPU | NVIDIA RTX 4090 24 GB, driver 591.86 (CUDA 13.1) | — |
| llama.cpp | release b11178 `win-cuda-12.4-x64` | `E:\llama.cpp\` |
| LLM | `Qwen3-14B-Q4_K_M.gguf` (Qwen/Qwen3-14B-GGUF) | `E:\llama.cpp\models\` |
| ASR | Faster-Whisper 1.2.1 + `Systran/faster-whisper-large-v3` | WSL HF cache `/root/.cache/huggingface` |
| TTS | faster-qwen3-tts 0.5.2 (CUDA graphs) + `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | venv `/root/cyber_gf/.venv-tts-fast`, HF cache |
| Avatar | LiveTalking (commit b3e7490, unmodified) + Wav2Lip `wav2lip256` | `/root/livetalking/LiveTalking` |
| Asset tooling (optional) | ComfyUI portable v0.37.0 + Wan2.2 14B i2v | `E:\ComfyUI_windows_portable\` |

Versions, hashes and sources: [docs/components.md](docs/components.md).

---

## Installation / reproduction
Tested on the reference machine only. Each step reuses a script from this repo.

**1. Windows side**
- Enable WSL, `wsl --install -d Ubuntu-24.04`, then copy `config/wslconfig.example` to `%USERPROFILE%\.wslconfig` and run `wsl --shutdown`.
- llama.cpp: download `llama-b11178-bin-win-cuda-12.4-x64.zip` and `cudart-llama-bin-win-cuda-12.4-x64.zip` from https://github.com/ggml-org/llama.cpp/releases and unzip both into one folder (default `E:\llama.cpp`). Use the CUDA 12.4 build unless your driver supports a newer CUDA.
- Model: `Qwen3-14B-Q4_K_M.gguf` from https://huggingface.co/Qwen/Qwen3-14B-GGUF → `E:\llama.cpp\models\`.
- If your paths differ, copy `config\paths.local.example.bat` to `config\paths.local.bat` and edit it.
- Check it: run `scripts\windows\start_llama.bat`, then `python scripts\windows\llm_test.py`.

**2. WSL side (Ubuntu-24.04)**: run each command as `wsl -d Ubuntu-24.04 -- bash <repo-in-wsl>/scripts/wsl/<script>`
- `lt_install.sh`: apt deps, uv, LiveTalking clone, `.venv-lt` (Python 3.10, torch 2.5.0 cu124)
- `lt_setup_weights.sh`: wav2lip256 weights and the official sample avatar (LiveTalking's Google Drive)
- `tts_fast_install.sh`: `.venv-tts-fast` (torch 2.8.0 cu128, faster-qwen3-tts 0.5.2, **transformers 5.15.1**, faster-whisper 1.2.1)
- Models: `hf download Systran/faster-whisper-large-v3` and `hf download Qwen/Qwen3-TTS-12Hz-1.7B-Base` (as in `voice_install.sh`; otherwise they download on first start)
- If your paths differ: copy `config/paths.local.example.env` to `config/paths.local.env`.

**3. Voice reference**
- Put a clean 5–15 s single-speaker recording in `assets/ref.wav`, and its **exact** transcript in `config/ref_text.txt`.

**4. Avatar**
- (Optional) Regenerate `assets/avatar/idle.mp4`:
  - run `scripts\windows\start_comfyui.bat`;
  - copy the avatar image as PNG to ComfyUI's `input\xiaoya_avatar.png`;
  - run `python scripts\windows\wan_idle.py`.
- Build the LiveTalking avatar: start LiveTalking (`lt_test_start.sh` or `start_step12_v2.bat`), then run `lt_build_avatar.sh` to create `wav2lip256_myavatar` from `assets/avatar/idle.mp4`.

**5. Run** the [Quick Start](#quick-start-reference-machine).

## Configuration
| File | What it sets |
|---|---|
| `config/paths.env` | WSL paths: `WSL_DISTRO`, `LT_DIR`, `LT_VENV`, `LT_AVATAR_ID`, `LT_STUN`, `TTS_FAST_VENV`, `HF_HOME`, logs. The project root is auto-detected. Override in `config/paths.local.env`. |
| `config/paths.bat` | Windows paths: `WSL_DISTRO`, `LLAMA_DIR`, `LLAMA_MODEL`, `LLAMA_ARGS` (`-c 8192 -fa on --temp 1.0 --top-p 0.95`, port 8090), `COMFY_DIR`. The project root and its WSL path are auto-detected. Override in `config/paths.local.bat`. |
| `config/voice.json` | ASR model, device, compute type and language; LLM URL and sampling; TTS model, dtype, attention; reference voice files |
| `config/step12_v2.json` | voice service port (8020), TTS `chunk_size` (8), `append_silence` (false), LLM→TTS mode (`full` or `sentence`), LiveTalking URL and avatar ID |
| `config/step12.json` | Step 12 MVP (fallback) settings |
| `config/persona.txt` | LLM system prompt (the tutorial's persona) |
| `assets/ref.wav`, `config/ref_text.txt` | voice-cloning reference and its exact transcript |
| `config/components.json` | informational inventory of the reference machine's external components |

**Ports:**
- 8090: llama-server (Windows)
- 8010: LiveTalking + adapter + imood page (WSL)
- 8020: voice service (WSL)

**WSL distro:** every launcher uses `wsl -d %WSL_DISTRO%` (default `Ubuntu-24.04`), and the WSL scripts refuse to run anywhere else.

## Runtime details
- **Model persistence:** `voice_service.py` loads Faster-Whisper and FasterQwen3TTS once, captures the CUDA graphs, and warms the reference prompt at startup. llama-server and LiveTalking also stay loaded, so each question pays only inference time.
- **Streaming path:** TTS yields 0.64 s chunks (`chunk_size=8`), which go through **one** stateful `soxr` 24→16 kHz resampler per answer and an in-memory queue. A sender thread streams them as **one chunked HTTP POST per answer** to `/cyber_gf/audio_stream`. Generation never waits for playback.
- **LiveTalking adapter** (`scripts/wsl/livetalking_cyber_gf.py`):
  - It runs the **unmodified** LiveTalking `app.py` and registers project routes at startup (`/cyber_gf/audio_stream`, `/cyber_gf/utt/{id}`, `/cyber_gf/stats`, `/imood/`).
  - It turns the PCM stream into 20 ms frames in LiveTalking's audio queue: one `start` event, one `end` event, the same pattern as LiveTalking's own streaming TTS integration.
  - Frames are enqueued atomically per received block, after a 480 ms pre-roll, which prevents start-of-utterance silence.
  - It reports underruns, queue depth, fps, and actual playback start and end.
- **Why upstream stays unmodified:** LiveTalking (latest `main`) has no continuous-audio endpoint. Adding routes at runtime keeps upgrades and the MVP path (`/humanaudio`) intact. Nothing in the LiveTalking checkout is edited (`git status` is clean).

## Performance (reference machine only)
RTX 4090 24 GB, WSL2 Ubuntu-24.04, `assets/test.wav` (3.84 s), 5 consecutive runs, default `full` mode. Seconds from request received:

| Stage | Measured |
|---|---|
| ASR done (Faster-Whisper large-v3) | 0.86–1.36 s |
| LLM first token / done (Qwen3-14B Q4_K_M) | 0.93–1.46 s / 1.39–1.85 s |
| TTS first chunk (= first audio into LiveTalking) | 1.80–2.32 s |
| **Avatar speaking start** | **2.95–3.35 s (mean 3.15 s)**; the Step 12 MVP (non-streaming) was 6.91 s |
| TTS speed (fixed-text benchmark) | RTF 0.29 (3.4× real time); first chunk 0.31 s after TTS start |
| LiveTalking | final 25.0 fps, inference 98–115 fps; 0 underruns / 0 overflows (5 of 5 runs, and 5 of 5 again after a cold start via the repo launchers: speaking start 2.87–3.70 s) |
| VRAM (full stack) | about 23.3 GB of 24 GB (llama-server ~10.0 GB; voice service + LiveTalking ~9–11 GB; desktop ~1.5 GB) |

Evidence and method: [docs/step12_v2.md](docs/step12_v2.md), [docs/perf_tts_fast.md](docs/perf_tts_fast.md), [docs/perf_tts_investigation.md](docs/perf_tts_investigation.md).

## Known issues
- **Remote-desktop viewing can stutter** even though recordings are smooth and in sync and the server renders a steady 25 fps. The imood page shows live WebRTC receive stats (fps, dropped frames, freezes, jitter) under 診斷資訊 for diagnosis.
- **VRAM is tight:** the full stack uses about 23.3 of 24 GB. Stop other GPU-heavy services first. On the reference machine that means a CosyVoice TTS on :8001 and an older service on :8000, which start automatically after a reboot or login.
- **Default WSL distro trap:** the reference machine's default distro is Ubuntu-22.04, where none of the paths exist. Always use `wsl -d Ubuntu-24.04`, which all launchers do.
- **First start:** the first run after installing downloads the HF models (about 7 GB) if they aren't cached.
- Avatar speech starts about 1.1 s after the first audio frame reaches LiveTalking. This is LiveTalking's internal buffering, and it's now the largest latency term.

## Project structure
```
cyber_gf/
├── README.md                 this file
├── .gitignore
├── config/                   paths (defaults + *.local.example templates), voice / Step 12 settings, persona, ref text, wslconfig template
├── scripts/
│   ├── windows/              start/stop launchers, llama/ComfyUI starters, MVP launcher, idle.mp4 generator, LLM test, benchmark and download tools
│   └── wsl/                  voice service, LiveTalking adapter, start/stop, install scripts (LiveTalking, weights, avatar, voice envs), MVP and Step 9 tools
│       └── perf/             benchmarks and the headless end-to-end test (outputs go to outputs/, gitignored)
├── web/                      imood_ai.html, avatars.json (served by the adapter at /imood/)
├── assets/                   ref.wav (voice reference), test.wav (sample question), avatar/ (image + idle.mp4), test_frames/ (evidence images)
└── docs/                     architecture, Step 12 v2 / MVP / Step 9, TTS performance, components, deviations, progress log
```
`outputs/` and `logs/` are created at runtime and are gitignored.

## Documentation
| Doc | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Current architecture, asset preparation, fallbacks |
| [docs/step12_v2.md](docs/step12_v2.md) | Streaming design, LiveTalking source analysis, adapter, measurements, start/stop, fallback |
| [docs/step12_mvp.md](docs/step12_mvp.md) | Non-streaming MVP (`/humanaudio`) |
| [docs/step9_voice_test.md](docs/step9_voice_test.md) | First voice pipeline (original qwen-tts backend) |
| [docs/perf_tts_investigation.md](docs/perf_tts_investigation.md) | Why official qwen-tts was slow (launch-bound, GPU 17% busy) |
| [docs/perf_tts_fast.md](docs/perf_tts_fast.md) | faster-qwen3-tts evaluation, before/after, quality check |
| [docs/components.md](docs/components.md) | External components, versions, sources |
| [docs/deviations.md](docs/deviations.md) | Differences from the original tutorial |
| [docs/status.md](docs/status.md) | Step-by-step progress log (Chinese) |

## Further development (not part of the working core)
- Microphone input and VAD (end-of-speech detection)
- Interruption / barge-in (LiveTalking already exposes `/interrupt_talk`)
- UI polish for the imood page
- More robust service management (health checks, auto-restart, a single supervisor)
- Reducing LiveTalking's internal ~1.1 s buffering latency
