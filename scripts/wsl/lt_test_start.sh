#!/usr/bin/env bash
# Start LiveTalking in the background (test use only).
# Usage: lt_test_start.sh [avatar_id]   (default: wav2lip256_myavatar; official sample: wav2lip256_avatar1)
source "$(dirname "$0")/../../config/paths.env"
AVATAR_ID="${1:-$LT_AVATAR_ID}"
[ -d "$LT_DIR" ] && [ -x "$LT_VENV/bin/python" ] || { echo "LiveTalking not found at $LT_DIR -- run inside WSL distro $WSL_DISTRO (wsl -d $WSL_DISTRO)"; exit 1; }
cd "$LT_DIR" && source "$LT_VENV/bin/activate" || exit 1
mkdir -p "$LT_LOG_DIR"
pkill -f "app.py --transport webrtc" ; sleep 1
setsid nohup python app.py --transport webrtc --model wav2lip --avatar_id "$AVATAR_ID" \
  --stun "$LT_STUN" > "$LT_LOG_DIR/lt_test.log" 2>&1 < /dev/null &
for i in $(seq 1 90); do sleep 2; grep -q -i -E "start http server|Traceback|Error" "$LT_LOG_DIR/lt_test.log" && break; done
tail -n 30 "$LT_LOG_DIR/lt_test.log"
