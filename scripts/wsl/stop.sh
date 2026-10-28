#!/usr/bin/env bash
# Stop every service of either mode (LiveTalking, BERT emotion service, voice service). llama-server: scripts\windows\stop.bat
source "$(dirname "$0")/../../config/paths.env"
cd "$PROJECT_WSL" && exec "$TTS_FAST_VENV/bin/python" -m imood.launcher stop
