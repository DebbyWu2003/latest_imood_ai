#!/usr/bin/env bash
# Stop Step 12 v2 WSL services (LiveTalking launcher + voice service). llama-server runs on Windows:
# scripts\windows\stop_avatar_services.bat stops it too.
pkill -f "livetalking_cyber_gf.py" 2>/dev/null && echo "LiveTalking (v2) stopped" || echo "LiveTalking (v2) not running"
pkill -f "scripts/wsl/voice_service.py" 2>/dev/null && echo "voice service stopped" || echo "voice service not running"
