#!/usr/bin/env bash
# Stop the WSL avatar services (LiveTalking launcher + voice service). llama-server runs on Windows:
# scripts\windows\stop_avatar_services.bat stops it too.
pkill -f "livetalking_cyber_gf.py" 2>/dev/null && echo "LiveTalking stopped" || echo "LiveTalking not running"
pkill -f "scripts/wsl/voice_service.py" 2>/dev/null && echo "voice service stopped" || echo "voice service not running"
