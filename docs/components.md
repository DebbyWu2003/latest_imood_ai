# External components (not in this repository)

Everything below is installed outside the repo. Paths are the defaults from `config/paths.env` (WSL) and `config/paths.bat` (Windows); override them in `config/paths.local.env` / `config/paths.local.bat`. A machine-readable inventory is in `config/components.json`.

| Component | Version used | Default location | How it was installed | Needed for |
|---|---|---|---|---|
| llama.cpp `llama-server.exe` | release **b11178**, `win-cuda-12.4-x64` + cudart 12.4 | `E:\llama.cpp\` | GitHub release zip | demo (LLM, :8090) |
| Qwen3-14B GGUF | `Qwen3-14B-Q4_K_M.gguf` (9.0 GB, SHA256 `500a8806…6b81f0`) | `E:\llama.cpp\models\` | https://huggingface.co/Qwen/Qwen3-14B-GGUF | demo |
| WSL distro | **Ubuntu-24.04** (user `root`) | — | `wsl --install -d Ubuntu-24.04` | demo |
| LiveTalking | commit **b3e7490**, source unmodified | `/root/livetalking/LiveTalking` | `scripts/wsl/lt_install.sh` | demo (:8010) |
| LiveTalking venv | Python 3.10, torch 2.5.0+cu124 | `…/LiveTalking/.venv-lt` | `lt_install.sh` | demo |
| wav2lip256 weights + sample avatar | from LiveTalking's Google Drive | `…/LiveTalking/models/wav2lip.pth`, `data/avatars/` | `scripts/wsl/lt_setup_weights.sh` | demo |
| Custom avatar `wav2lip256_myavatar` | built from `assets/avatar/idle.mp4` | `…/LiveTalking/data/avatars/wav2lip256_myavatar` | `scripts/wsl/lt_build_avatar.sh` | demo |
| Voice env (main) | Python 3.12, torch 2.8.0+cu128, faster-qwen3-tts 0.5.2, qwen-tts-hf 0.1.1.post1, transformers **5.15.1**, faster-whisper 1.2.1 | `/root/cyber_gf/.venv-tts-fast` | `scripts/wsl/tts_fast_install.sh` | demo (:8020) |
| Faster-Whisper model | `Systran/faster-whisper-large-v3` (2.9 GB) | HF cache `/root/.cache/huggingface` | downloaded on first use / `voice_install.sh` | demo |
| Qwen3-TTS model | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (4.3 GB) | HF cache | downloaded on first use / `voice_install.sh` | demo |
| Voice env (benchmark) | Python 3.12, official `qwen-tts` 0.1.1, transformers 4.57.3 | `/root/cyber_gf/.venv-voice` | `scripts/wsl/voice_install.sh` | optional |
| ComfyUI portable | v0.37.0 `nvidia_cu126` | `E:\ComfyUI_windows_portable\` | GitHub release | only to regenerate `idle.mp4` |
| Wan2.2 14B i2v models | Comfy-Org repackaged fp8 (about 36 GB) | ComfyUI `models\` | https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged | only to regenerate `idle.mp4` |
| WSL tools | uv 0.12.19, apt `dos2unix ffmpeg libgl1 libglib2.0-0` | `/root/.local/bin/uv` | `lt_install.sh` | setup |

Runtime logs (outside the repo): `/root/lt_logs/lt_cyber_gf.log` (LiveTalking + adapter). The voice service log goes to `logs/voice_service.log` inside the project, which is gitignored.
