#!/usr/bin/env bash
# Start the services for one mode (Ubuntu-24.04):   bash scripts/wsl/start.sh [vendor|full]
# Mode: argument > env MODE > imood/pipeline.yaml. vendor = accepted vendor setup (LiveTalking only);
# full = LiveTalking + BERT emotion service + voice service (llama-server runs on Windows: scripts\windows\start.bat).
source "$(dirname "$0")/../../config/paths.env"
if [ "$WSL_DISTRO_NAME" != "$WSL_DISTRO" ]; then echo "ERROR: run in WSL distro $WSL_DISTRO (wsl -d $WSL_DISTRO)"; exit 1; fi
cd "$PROJECT_WSL" && exec "$TTS_FAST_VENV/bin/python" -m imood.launcher start ${1:+"$1"}
