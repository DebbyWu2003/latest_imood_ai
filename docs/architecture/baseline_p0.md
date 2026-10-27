# P0 基準紀錄（改寫前）

- 日期：2026-10-27　分支：`refactor/own-architecture`（基底 commit `fdcb9d7`）
- `scripts/wsl/vendor_voice_test.py`：**ALL PASS**（廠商協定模擬測試）
- LiveTalking :8010 運作中，`/cyber_gf/stats` 回 `code 0`，avatar `wav2lip256_emo_calm`，`lt_fps final ≈ 25`
- 對外路徑（改寫期間必須保持可用，廠商／前端／接收端都依賴）：
  - `WS  /cyber_gf/voice`（廠商即時語音）
  - `POST /cyber_gf/audio_stream`（本機語音服務推音訊）
  - `GET  /cyber_gf/utt/{utt}`、`GET /cyber_gf/stats`
  - `GET|POST /cyber_gf/emotion`（情緒按鈕）
  - `GET  /imood/*`（前端頁面）
- 實機驗收行為：廠商一句話 → 先換臉（淡入淡出）→ 說話 → 說完停在該情緒；按鈕切換三種情緒
