#!/usr/bin/env bash
# Build the LiveTalking avatar (default wav2lip256_myavatar) from assets/avatar/idle.mp4 via LiveTalking's own
# avatar API (the same request its avatar.html form sends, with the form defaults).
# Needs LiveTalking running (lt_test_start.sh or start_avatar_services.sh). Usage: lt_build_avatar.sh [video] [avatar_id]
set -e
source "$(dirname "$0")/../../config/paths.env"
VIDEO="${1:-$PROJECT_WSL/assets/avatar/idle.mp4}"; AVATAR="${2:-$LT_AVATAR_ID}"
R=$(curl -s -F model=wav2lip -F "avatar_id=$AVATAR" -F "video_file=@$VIDEO" -F img_size=256 -F bbox_shift=0 \
        -F "pads=0 10 0 0" -F face_det_batch_size=4 "http://localhost:$LT_PORT/api/avatar/task")
echo "$R"; TASK=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['task_id'])")
for i in $(seq 1 60); do
  S=$(curl -s "http://localhost:$LT_PORT/api/avatar/task/$TASK"); echo "$S" | grep -q -E '"(completed|failed)"' && break; sleep 5
done
echo "$S"; ls -d "$LT_DIR/data/avatars/$AVATAR"
