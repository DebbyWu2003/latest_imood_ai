# Deviations from the original tutorial

The project started as a faithful reproduction of 零度解说's tutorial (https://www.freedidi.com/24984.html). These are the places where it differs, and why.

## Components and installation
| # | Tutorial | This project | Reason |
|---|---|---|---|
| 1 | llama.cpp from a Quark mirror, version unspecified | Official GitHub release **b11178**, `win-cuda-12.4` | The mirror requires a login. The CUDA 13.4 build needs a newer driver than the reference machine's (CUDA 13.1). |
| 2 | Model `Qwen3-14B-Instruct-Q4_K_M.gguf` (Quark / forum links) | Official `Qwen/Qwen3-14B-GGUF` → `Qwen3-14B-Q4_K_M.gguf` | The links are login-gated and no official file has the tutorial's name. `config/paths.bat` uses the real filename. |
| 3 | ComfyUI desktop app | ComfyUI **portable** v0.37.0 (same project) | The desktop app's GUI installer can't be scripted. |
| 4 | "Select Wan2.2", but the parameter table lists fps 25 and `prompt_enhance` | **Wan2.2 14B i2v** template, 704×896, 5 s, 16 fps (81 frames) | The table's fields match ComfyUI's LTX-2.3 template, while the text names Wan2.2. The model named in the text was used, via `scripts/windows/wan_idle.py`. |
| 5 | LiveTalking weights from the author's package | LiveTalking's official Google Drive (`scripts/wsl/lt_setup_weights.sh`) | The package wasn't available. The tutorial names this source as the alternative. |
| 6 | Avatar built with the `avatar.html` form | Same backend API (`scripts/wsl/lt_build_avatar.sh`) with the form's defaults | Scriptable |

## The author's download package (Steps 6, 9, 12) was not used
The package (`install-voice.sh`, `start-voice.sh`, `start-livetalking.sh`, `avatar-sync.js`, `ref.wav`) is login-gated, and its patches are undocumented. Those steps were **rebuilt from open-source components**:

| Tutorial | This project |
|---|---|
| `install-voice.sh` → "speech-to-speech" with VAD / Whisper / Qwen3-TTS, web UI on :7860 | `scripts/wsl/tts_fast_install.sh` (Faster-Whisper large-v3 + faster-qwen3-tts) and `scripts/wsl/voice_service.py` on :8020 |
| `avatar-sync.js`: audio forwarding and avatar layer | `scripts/wsl/livetalking_cyber_gf.py`: runtime adapter that streams PCM into LiveTalking's frame queue |
| `start-voice.sh` / `start-livetalking.sh` | `scripts/wsl/start_step12_v2.sh` and `scripts/windows/start_step12_v2.bat` |
| Voice orb page on :7860 with microphone and VAD | Browser page `web/imood_ai.html` (microphone button or WAV upload; no VAD yet) |
| Reference voice from the package | `assets/ref.wav` + `config/ref_text.txt` supplied by the project owner |

## Performance-driven changes
| Change | Evidence |
|---|---|
| Official `qwen-tts` 0.1.1 (RTF ≈ 3.1) → **faster-qwen3-tts 0.5.2** (CUDA graphs + StaticCache, RTF ≈ 0.29), same model, reference and sampling defaults; `append_silence=False` to match upstream | [perf_tts_investigation.md](perf_tts_investigation.md), [perf_tts_fast.md](perf_tts_fast.md) |
| transformers pinned to **5.15.1** in `.venv-tts-fast` (5.17.0 breaks `qwen-tts-hf`: `MimiConfig has no rope_theta`) | [perf_tts_fast.md](perf_tts_fast.md) |
| Streaming TTS (`chunk_size=8`) into LiveTalking's frame queue instead of posting one WAV per answer | [step12_v2.md](step12_v2.md) |
| LLM → TTS in **full** mode by default (sentence mode measured slower on a shared GPU) | [step12_v2.md](step12_v2.md) |
| Frontend page: STUN off by default (like upstream `index.html`), ICE gathering capped at 2 s | An unreachable STUN server stalled the connect button for tens of seconds |

## Machine setup
- `.wslconfig` uses the tutorial's values (mirrored networking + `hostAddressLoopback`); see `config/wslconfig.example`.
- Everything WSL-side runs in **Ubuntu-24.04**, which isn't the reference machine's default distro. Every launcher passes `-d Ubuntu-24.04` explicitly.
