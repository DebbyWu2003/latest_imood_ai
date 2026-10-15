# Vendor real-time voice receiver

Receives the vendor's real-time speech + emotion over WebSocket and plays it on the avatar. Code: `scripts/wsl/vendor_voice.py`, mounted by `scripts/wsl/livetalking_cyber_gf.py`. Vendor spec: https://websocket-audio-tester.cielsi.workers.dev/doc. Emotion hand-off to the emotion owner: [emotion_interface.md](emotion_interface.md).

## Endpoint
`ws://localhost:8010/cyber_gf/voice?token=<VENDOR_VOICE_TOKEN>` (same port as LiveTalking). Set the token before starting the services, e.g. in `config/paths.local.env`: `export VENDOR_VOICE_TOKEN=...`. Without it every connection is rejected (4001).

## Flow
vendor `utterance.start` / PCM chunks / `utterance.end` → int16 ÷ 32768 → 20 ms float32 frames → LiveTalking ASR queue (same items as `/cyber_gf/audio_stream`).
LiveTalking announces when a start/end frame is actually played; the receiver then sends `playback.started` / `playback.ended` back to the vendor and a `{"type":"emotion",...}` message to `/sse` listeners.

## Behaviour to know
- Plays on the **most recently opened** LiveTalking session (the vendor protocol has no sessionid). No session → `error: no_session`.
- Latency: 480 ms pre-roll, then 200 ms blocks (`VENDOR_PREROLL_FRAMES`, `VENDOR_BLOCK_FRAMES`) so a real-time sender does not starve the lip-sync thread.
- Close codes: 4001 bad token/origin, 4002 replaced by a newer sender, 4003 unsupported version. Allowed origin: the vendor sender page (+ `VENDOR_ALLOWED_ORIGINS`, comma separated).
- Aborted / superseded / disconnected utterances keep the audio already queued and are closed with an end frame.
- Production needs a public `wss://` URL with a CA-signed certificate (the sender page is HTTPS). Cloud deployment is owned by another teammate; this receiver is one route on the LiveTalking port (8010), so the `wss://` proxy only has to forward `/cyber_gf/voice` as a WebSocket. Not set up yet.

## Tests
- Protocol, no GPU: `/root/livetalking/LiveTalking/.venv-lt/bin/python scripts/wsl/vendor_voice_test.py` (WSL).
- End to end: start the services, open the imood page and click 開始視訊, then in the vendor sender choose 外部接收端, endpoint `ws://localhost:8010/cyber_gf/voice`, your token, 連線, 傳送.

## Test results (2026-10-15, local, vendor sender page + real avatar)
Setup: only LiveTalking was started (no voice service / llama), endpoint `ws://localhost:8010/cyber_gf/voice`, real-time sending mode.

| Check (vendor checklist, section 10) | Result |
|---|---|
| Wrong token / wrong origin closes with 4001, live sender unaffected | PASS (protocol test) |
| Single utterance plays, sender log shows 播放中 → 播放完成 | PASS |
| 3 utterances with different emotions queued back to back (angry, fear, happy) play in order, emotion matches | PASS (server log) |
| Stop mid-transmission (`aborted`) | PASS: sender shows 已停止, utterance closed with an end frame, queue empty |
| Receiver restart: sender shows reconnecting, then reconnects by itself; after reopening the video page, playback works | PASS |
| Lip-sync follows the voice, no audio stutter | PASS (observed by the user) |
| Idle 10 minutes, then send | not tested yet |
| Production `wss://` endpoint | not set up (cloud teammate) |

Not covered: the emotion message on `/sse` was only verified by the protocol test; nothing consumes it yet (emotion teammate). Only the real-time sending mode was tried with the real avatar; the "一次傳送" (burst) mode was not.
