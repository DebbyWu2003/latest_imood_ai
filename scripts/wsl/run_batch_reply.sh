#!/usr/bin/env bash
# Step 12 MVP: test.wav -> ASR -> llama.cpp -> fast Qwen3-TTS -> reply.wav -> LiveTalking /humanaudio
# Prereqs: llama-server on :8090 (Windows), LiveTalking on :8010 with wav2lip256_myavatar,
#          browser page http://localhost:8010/index.html connected (avatar video playing).
# Usage (inside Ubuntu-24.04):  bash run_batch_reply.sh [--in file.wav] [--session <id>]
# From Windows:                 scripts\windows\run_batch_reply.bat [same args]
set -e
source "$(dirname "$0")/../../config/paths.env"
if [ "$WSL_DISTRO_NAME" != "$WSL_DISTRO" ]; then
  echo "ERROR: must run in WSL distro $WSL_DISTRO (current: '${WSL_DISTRO_NAME:-not WSL}')."
  echo "       Use: wsl -d $WSL_DISTRO -- bash $0"; exit 1
fi
source "$TTS_FAST_VENV/bin/activate"
export HF_HOME
SP=$(python -c "import site;print(site.getsitepackages()[0])")
export LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib:${LD_LIBRARY_PATH}"
python "$PROJECT_WSL/scripts/wsl/batch_reply.py" "$@"
