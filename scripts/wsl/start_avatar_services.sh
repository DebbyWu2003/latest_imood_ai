#!/usr/bin/env bash
# Start the avatar services in WSL (distro from config/paths.env, default Ubuntu-24.04):
#   1. LiveTalking (unmodified app.py) via the project launcher/adapter  -> :8010
#   2. voice service (Whisper + FasterQwen3TTS) -> :8020, ONLY if LEGACY_VOICE=1 (config/paths.env)
# Vendor speech arrives at ws://localhost:8010/cyber_gf/voice (needs VENDOR_VOICE_TOKEN, see docs/vendor_voice_receiver.md).
# llama-server (:8090, only for LEGACY_VOICE=1) runs on Windows (scripts\windows\start_llama.bat).
set -e
source "$(dirname "$0")/../../config/paths.env"
if [ "$WSL_DISTRO_NAME" != "$WSL_DISTRO" ]; then echo "ERROR: run in WSL distro $WSL_DISTRO (wsl -d $WSL_DISTRO), not '${WSL_DISTRO_NAME:-not WSL}'"; exit 1; fi
LOG="$PROJECT_WSL/logs"; mkdir -p "$LOG" "$LT_LOG_DIR"
[ "$LEGACY_VOICE" = 1 ] && { curl -s --max-time 3 http://localhost:8090/health | grep -q ok || echo "WARNING: llama-server not reachable on :8090 (start scripts\windows\start_llama.bat)"; }

# stop anything already on these ports (an earlier LiveTalking run)
pkill -f "app.py --transport webrtc" 2>/dev/null || true
pkill -f "livetalking_cyber_gf.py" 2>/dev/null || true
pkill -f "scripts/wsl/voice_service.py" 2>/dev/null || true
sleep 2

# 1. LiveTalking + adapter (same args as lt_test_start.sh)
( cd "$LT_DIR" && source "$LT_VENV/bin/activate" && export LT_DIR && \
  exec setsid nohup python "$PROJECT_WSL/scripts/wsl/livetalking_cyber_gf.py" --transport webrtc --model wav2lip \
    --avatar_id "$LT_AVATAR_ID" --stun "$LT_STUN" ) > "$LT_LOG_DIR/lt_cyber_gf.log" 2>&1 < /dev/null &

# 2. voice service (only with LEGACY_VOICE=1; otherwise speech arrives via /cyber_gf/voice)
if [ "$LEGACY_VOICE" = 1 ]; then
( source "$TTS_FAST_VENV/bin/activate" && export HF_HOME && \
  SP=$(python -c "import site;print(site.getsitepackages()[0])") && \
  export LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib:${LD_LIBRARY_PATH}" && \
  exec setsid nohup python "$PROJECT_WSL/scripts/wsl/voice_service.py" ) > "$LOG/voice_service.log" 2>&1 < /dev/null &
fi

echo "waiting for services..."
for i in $(seq 1 120); do
  lt=$(curl -s --max-time 1 http://localhost:8010/cyber_gf/stats | grep -c '"code": 0' || true)
  vs=1; [ "$LEGACY_VOICE" = 1 ] && vs=$(curl -s --max-time 1 http://localhost:8020/api/health | grep -c '"ok": true' || true)
  [ "$lt" = 1 ] && [ "$vs" = 1 ] && break
  sleep 2
done
[ "$lt" = 1 ] && echo "LiveTalking+adapter: OK  (log $LT_LOG_DIR/lt_cyber_gf.log)" || echo "LiveTalking: NOT READY (see $LT_LOG_DIR/lt_cyber_gf.log)"
if [ "$LEGACY_VOICE" = 1 ]; then [ "$vs" = 1 ] && echo "voice service:       OK  (log $LOG/voice_service.log)" || echo "voice service: NOT READY (see $LOG/voice_service.log)"; else echo "legacy voice service: off (LEGACY_VOICE=0); vendor voice: ws://localhost:8010/cyber_gf/voice"; fi
echo "Open: http://localhost:8010/imood/imood_ai.html"
