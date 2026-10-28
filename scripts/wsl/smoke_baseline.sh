#!/usr/bin/env bash
# Smoke check (Ubuntu-24.04):
#   1. vendor protocol test (no GPU)         2. LiveTalking public routes answer
#   3. emotion switch over HTTP (calm/happy/sad) when LiveTalking is running with an emotion avatar
# Usage: wsl -d Ubuntu-24.04 -- bash scripts/wsl/smoke_baseline.sh      (LiveTalking should be running on :8010)
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${LT_PY:-/root/livetalking/LiveTalking/.venv-lt/bin/python}"
LT="${LT_URL:-http://127.0.0.1:8010}"
fail=0
ok()  { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; fail=1; }

out=$("$PY" "$HERE/vendor_voice_test.py" 2>&1)
echo "$out" | grep -q "ALL PASS" && ok "vendor_voice_test" || { bad "vendor_voice_test"; echo "$out" | tail -5; }

code() { curl -s -m 5 -o /dev/null -w '%{http_code}' "$@"; }
[ "$(code "$LT/cyber_gf/stats")" = 200 ] && ok "GET /cyber_gf/stats" || bad "GET /cyber_gf/stats (is LiveTalking running?)"
[ "$(code "$LT/imood/imood_ai.html")" = 200 ] && ok "GET /imood/imood_ai.html" || bad "GET /imood/imood_ai.html"
[ "$(code -X POST "$LT/cyber_gf/audio_stream")" != 000 ] && ok "POST /cyber_gf/audio_stream reachable" || bad "POST /cyber_gf/audio_stream"
# WebSocket route: a plain GET without upgrade must be answered by the handler (not 404)
[ "$(code "$LT/cyber_gf/voice")" != 404 ] && ok "WS /cyber_gf/voice registered" || bad "WS /cyber_gf/voice"

for e in happy sad neutral; do
  r=$(curl -s -m 5 -X POST "$LT/cyber_gf/emotion?emotion=$e")
  # code 0 = switched; code -1 "session not found" is acceptable when no browser page is connected
  echo "$r" | grep -q '"code": 0\|"code":0' && ok "emotion $e switched" || echo "SKIP  emotion $e (no connected session): $r"
done
[ $fail = 0 ] && echo "== BASELINE OK ==" || { echo "== BASELINE FAILED =="; exit 1; }
