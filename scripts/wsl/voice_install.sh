#!/usr/bin/env bash
# Voice environment: Python 3.12 env with faster-whisper large-v3 + Qwen3-TTS 1.7B Base.
# Installs into WSL (not /mnt/c). Does not touch llama.cpp / ComfyUI / LiveTalking.
set -ex
source "$(dirname "$0")/../../config/paths.env"
export PATH="$HOME/.local/bin:$PATH" UV_HTTP_TIMEOUT=300 UV_HTTP_RETRIES=10
retry(){ for i in $(seq 1 10); do "$@" && return 0; echo "retry $i: $*"; sleep 5; done; return 1; }

mkdir -p "$(dirname "$VOICE_VENV")"
[ -d "$VOICE_VENV" ] || uv venv --python 3.12 "$VOICE_VENV"
source "$VOICE_VENV/bin/activate"

# 1. PyTorch (CUDA 12.8 wheels; also provides cuBLAS / cuDNN 9 used by CTranslate2)
retry uv pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
# 2. Official packages (pin torch so the resolver cannot swap it)
retry uv pip install qwen-tts==0.1.1 faster-whisper==1.2.1 torch==2.8.0 torchaudio==2.8.0 \
  --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match

python - <<'PY'
import torch, transformers, ctranslate2, faster_whisper, qwen_tts
print("TORCH", torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))
print("transformers", transformers.__version__, "ctranslate2", ctranslate2.__version__,
      "faster_whisper", faster_whisper.__version__, "cuda devices (ct2)", ctranslate2.get_cuda_device_count())
PY

# 3. Models -> HF cache in WSL
export HF_HOME
retry hf download Systran/faster-whisper-large-v3
retry hf download Qwen/Qwen3-TTS-12Hz-1.7B-Base
du -sh "$HF_HOME"/hub/models--* 2>/dev/null
echo VOICE_INSTALL_DONE
