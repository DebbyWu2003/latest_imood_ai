# 兩種模式：使用、結構、測試、待確認項目

分支 `refactor/own-architecture`。程式碼集中在套件 `imood/`，單一設定檔 `imood/pipeline.yaml`。
設計背景與三方比較見 [architecture_v3_design.md](architecture_v3_design.md) 與 [architecture/architecture_compare.html](architecture/architecture_compare.html)。

## 1. 怎麼啟動

| | `vendor`（預設，驗收版） | `full`（完整版） |
|---|---|---|
| 啟動的服務 | LiveTalking（含轉接層） | LiveTalking ＋ BERT 情緒服務（UDP :8102）＋ 語音服務（:8020，麥克風 WS :8021）＋ llama-server（Windows :8090） |
| 語音來源 | 廠商 `ws://localhost:8010/cyber_gf/voice` | 瀏覽器錄音（`POST :8020/api/ask`）或串流麥克風（`ws://127.0.0.1:8021/ws/audio`） |
| 情緒來源 | 廠商給的情緒字串 | BERT 對回覆文字的分類（逾時用預設） |
| 下游 | 與 LiveTalking 同程序，直接入佇列 | TCP 分幀 → LiveTalking（127.0.0.1:8100） |

```bat
scripts\windows\start.bat vendor
scripts\windows\start.bat full
scripts\windows\stop.bat
```

WSL 內：`bash scripts/wsl/start.sh [vendor|full]`、`bash scripts/wsl/stop.sh`。
模式優先順序：命令列參數 > 環境變數 `MODE` > `imood/pipeline.yaml` 的 `mode`（預設 `vendor`）。
`vendor` 走的是**原本未改動**的 `scripts/wsl/start_step12_v2.sh`（`LEGACY_VOICE=0`），舊的 `start_step12_v2.bat` 也照常可用。

## 2. 程式結構（`imood/`）

```
pipeline.yaml        單一設定入口：mode、情緒（UDP 位址、逾時、預設）、下游（port、transport）、麥克風、VAD
launcher.py          依模式組出要啟動的服務清單（build_plan），start / stop / status
common/              labels（8 類→5 類→廠商字串→臉）、framing（BEGIN/AUDIO/END/STATUS）、config
intake/              vendor_ws（廠商 WS）、transcriber（ASR）、mic_ws（麥克風 WS）、mic_endpointer（VAD 斷句）、coalescer（續句合併）
dialogue/            llm_client（llama.cpp 串流）、segmenter（切句）
voice/               tts_voice（Qwen3-TTS 串流、重取樣）
emotion/             classifier、preprocess、labels、udp_service（BERT 服務）、udp_client（背景任務、逾時退回預設）
downstream/          framed_client（服務端）、framed_receiver（LiveTalking 內）、face_switcher（換臉）、livetalking_plugin（外掛路由）
service/             full 模式的語音服務：state、job、runner（一次問答的流程）、http_api、main
tests/               各模組測試
```

對外名稱沒有改：`/cyber_gf/voice`、`/cyber_gf/audio_stream`、`/cyber_gf/utt/{utt}`、`/cyber_gf/stats`、`/cyber_gf/emotion`、`/imood/*`、廠商協定的訊息型別與欄位、LiveTalking 介面、avatar id、既有環境變數。
舊路徑 `scripts/wsl/vendor_voice.py`、`livetalking_cyber_gf.py`、`voice_service.py` 留作轉接（shim），啟動腳本不用改。

## 3. full 模式一次問答怎麼走

```
麥克風 chunk → VAD 斷句 → ASR → 續句合併 → LLM（llama.cpp）→ 回覆文字 ─┬→ TTS（24k→16k）─┐
                                                                     └→ BERT（UDP，背景）  │
                                         第一塊音訊出來時：BERT 有結果就用，沒有就用預設，不等 ┘
→ BEGIN{session, utterance, emotion(廠商字串), text} → AUDIO*(PCM16 16k) → END
→ LiveTalking：依 emotion 換臉 → 切成 20 ms 幀入佇列 → Wav2Lip → WebRTC
← STATUS：received → playback_started → playback_ended（或 error）
```

TTS 與 BERT 同時拿到回覆文字。`full` 的 LLM→TTS 預設是等整段寫完再念（`config/step12_v2.json` 的 `llm_to_tts.mode`）；
要邊寫邊念改成 `sentence`。慢的時候先看 GPU 顯存（見 README 的 VRAM 說明）。

## 4. 測試

在語音環境（`/root/cyber_gf/.venv-tts-fast`，需 `webrtcvad-wheels`、`websockets`）：

| 指令 | 內容 | 需要服務 |
|---|---|---|
| `python -m imood.tests.test_common` | 標籤映射、框架、設定 | 否 |
| `python -m imood.tests.test_emotion_udp` | BERT UDP 服務／客戶端（假預測器） | 否 |
| `python -m imood.tests.test_framed` | framed 連線迴圈測試 | 否 |
| `python -m imood.tests.test_mic` | VAD、續句合併、`/ws/audio`（用 `assets/開心測試音檔.wav`） | 否 |
| `python -m imood.tests.test_launcher` | 各模式的服務清單 | 否 |
| `bash scripts/wsl/smoke_baseline.sh` | vendor 協定測試、路由、換臉（每一步改完都要跑） | LiveTalking |

## 5. 待確認的值

下列值目前用暫定值，設定在 `imood/pipeline.yaml`，確認後只改設定：

- BERT UDP port（8102）、逾時（300 ms）、逾時用的預設值（`neutral`）。
- 下游 framed port（8100）、麥克風 WS port（8021）。
- BEGIN 的 `emotion` 欄位型別：目前是廠商字串（`happy`、`sad`、`neutral`…）；若要改成 0–4 序號，只改 `downstream/framed_client` 呼叫處。
- AUDIO 的序號欄位：目前沒有（單一連線、依序送）。
- STATUS 欄位：目前 `{utterance, state}`，state 為 `received`、`playback_started`、`playback_ended`、`error`。
- 單一 TCP 連線上的並行與中止（目前一個連線一句話，播完即關）。
- BERT 取用的文字：目前是 TTS 拿到的第一段文字（`full` 模式即整段回覆）。
- 瀏覽器端的文字回顯。

## 6. 已知限制

- vendor 模式下 LiveTalking 內也會開 127.0.0.1:8100 的 framed 接收器（不影響廠商流程）；不要可設 `downstream.enabled: false`。
- full 模式的全部元件共用一張 24 GB 顯卡，顯存吃緊時 TTS 會慢於即時而斷音（與傳輸方式無關，實測 http / framed 相同）。
- `service/state.py` 仍從 `scripts/wsl/` 匯入舊函式（`load_cfg`、`discover_session`、`lt_get`），尚未搬進套件。
- 8 類 BERT 模型的標籤順序取自模型卡，無法從模型本身驗證。

## 7. 前端（`web/imood_ai.html`）

- 上方標籤「模式:vendor / full」唯讀，依語音服務（:8020）有沒有回應判斷；切換模式靠 `start.bat vendor|full`。
- 設定裡的「串流麥克風」（或網址 `?stream=1`）：full 模式下持續聆聽，伺服器自動斷句，每個想法產生一個回答。預設關閉，仍是點一下錄音、再點一下送出。
- 數字人回答期間前端不送麥克風音訊（避免把她自己的聲音當成問題），所以回答時無法插話；要插話需要前端回音處理與後端中斷回答，尚未實作。
