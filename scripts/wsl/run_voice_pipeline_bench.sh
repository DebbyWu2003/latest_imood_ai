#!/usr/bin/env bash
# Run the Step 9 voice pipeline test. Requires llama-server on :8090 (scripts\windows\start_llama.bat).
# Usage: run_voice_pipeline_bench.sh [--in path.wav] [--runs N] [--out path.wav]
source "$(dirname "$0")/../../config/paths.env"
source "$VOICE_VENV/bin/activate"
export HF_HOME
# CTranslate2 (faster-whisper) needs cuBLAS/cuDNN 9: reuse the ones shipped with the torch cu128 wheels
SP=$(python -c "import site;print(site.getsitepackages()[0])")
export LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib:${LD_LIBRARY_PATH}"
curl -s --max-time 3 http://localhost:8090/health | grep -q ok || { echo "llama-server not reachable on :8090"; exit 1; }
python "$PROJECT_WSL/scripts/wsl/voice_pipeline_bench.py" "$@"
