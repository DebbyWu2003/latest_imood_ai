# iMood 整合架構設計（v2 草案，尚未開始改寫）

> 分支：`refactor/own-architecture`（不進 `main`）。架構圖見 [architecture/architecture_compare.html](architecture/architecture_compare.html)（A 零度、B 早期自研、C 最終版）。
> 原則：**不改功能**。廠商已驗收的行為（廠商語音接收、情緒換臉、淡入淡出、說完停在該情緒）必須完全相同，改的只有分層、命名與啟動方式。
> 風格：命名與寫法沿用我們早期專案的習慣（`*_client`、`pipeline` 設定檔、背景任務失敗不影響主流程、失敗一律退回預設值），不沿用教學站的命名。

## 1. 設計來源

我們早期的系統（`imood-voice` ＋ `imood-emotion`）已經有完整的骨架：

```
麥克風 → VAD 斷句 → ASR → LLM → ┬─ TTS ─────────────┬→ 下游用戶端（彙整）→ JoyGen → 影像
                                 └─ BERT 情緒（平行）─┘
```

最終版沿用這個骨架，**模組換成較好的**（TTS 換 Qwen3-TTS、LLM 換 Qwen3-14B、影像換 LiveTalking＋Wav2Lip），
並加上「vendor 模式」（輸入改由廠商提供語音與情緒序號）。

## 2. 兩種模式（啟動時選擇，一個設定值）

| | 模式 `vendor`（預設，現行驗收版） | 模式 `full`（完整版） |
|---|---|---|
| 輸入 | 廠商給的語音（pcm16le 16 kHz）＋情緒序號 | 使用者說的話（瀏覽器麥克風） |
| 經過 | 旁路，不跑 ASR／LLM／TTS／BERT | VAD → ASR → LLM → 〔TTS ∥ BERT〕→ 下游用戶端 |
| 情緒來源 | 廠商序號 | BERT（逾時用預設序號） |
| 下游 | 與 LiveTalking 同程序直接入佇列（現行做法） | 下游用戶端 → TCP 分幀 → LiveTalking |
| 對應 | 現在的 `latest_imood_ai` | 早期系統＋LiveTalking（不含 JoyGen） |

兩種模式的**後段完全相同**：換臉（face_switcher）→ Wav2Lip → WebRTC（RTP）→ 瀏覽器。

## 3. 連線協定（與早期的差異）

| 連線 | 早期 | 最終版 |
|---|---|---|
| 瀏覽器 → 後端 | WebSocket | 同 |
| ASR → LLM → TTS | 同程序 | 同 |
| LLM（llama.cpp） | HTTP | 同（第三方介面） |
| **BERT ⇄ 用戶端** | **HTTP（每次新連線）** | **UDP**：請求 `{seq, text}`，回覆 `{seq, code, confidence}`；逾時用預設序號，不阻塞語音 |
| **下游（音訊＋情緒序號）** | **TCP 分幀 → JoyGen** | **TCP 分幀 → LiveTalking**（沿用 `BEGIN／AUDIO／END／STATUS` 協定） |
| 影像輸出 | 影片片段／RTP | WebRTC（RTP／SRTP） |

> 這次唯一的協定變更是 BERT 鏈路 HTTP → UDP（leader 的要求：不為傳一個序號開 HTTP 通道）。
> 音訊鏈路維持早期的 TCP 分幀：早期註解明確寫了「音訊不能丟包，少一個 sample 整條時間軸就位移」。

## 4. 要新建的東西

1. **下游用戶端**（對應早期 `joygen_client.py`）：彙整 TTS 的 PCM 與 BERT 的序號，送 BEGIN／AUDIO／END，收 STATUS。
   - 首個 PCM 塊到時取 BERT 結果，沒算完用預設，不等（早期 `_emotion_now` 的做法）。
2. **LiveTalking 端的接收器**：單一 TCP 連線、分幀收取，轉成現有的音訊佇列與事件（取代現在本機流程用的 HTTP 分塊 POST）。
3. **BERT UDP 服務**：把早期 `imood-emotion` 的分類器（前處理、模型、標籤映射）包成 UDP 服務；8 類 → 5 類 → 臉的映射集中一處。
4. **模式切換與設定**：單一設定檔（模式、位址、路徑），預設即交付版。

## 5. 目錄結構（目標，命名用我們自己的）

```
imood/
  pipeline.yaml              # 單一設定入口（沿用早期習慣）
  common/                    # 資料格式、標籤映射、框架協定（BEGIN／AUDIO／END／STATUS）
  intake/
    vendor_ws.py             # 廠商即時語音（現 vendor_voice.py）
    mic_endpointer.py        # VAD 斷句（早期 voice_asr 的斷句）
    transcriber.py           # ASR
  dialogue/
    llm_client.py            # llama.cpp
    persona.txt
  voice/
    tts_clone.py             # Qwen3-TTS 串流
    resample.py
  emotion/
    preprocess.py            # 早期 emotion/preprocess.py
    classifier.py            # 早期 emotion/classifier.py
    labels.py
    udp_service.py           # BERT UDP 服務
    udp_client.py            # 用戶端（背景任務、逾時退回預設）
  downstream/
    framed_client.py         # 下游用戶端（TCP 分幀，對應早期 joygen_client）
    framed_receiver.py       # LiveTalking 端接收器
    face_switcher.py         # 換臉＋淡入淡出（現 set_emotion／_install_fade）
    livetalking_plugin.py    # 外掛路由（現 livetalking_cyber_gf.py）
  assets/                    # avatar 素材與建立流程（現 wan_idle.py 等）
  web/
  ops/                       # 啟動／停止／健檢
  tests/
```

## 6. 改寫順序（每一步都驗證功能不變）

1. **P0 基準**：在分支上錄下基準——`vendor_voice_test.py` 15 項、情緒換臉實機流程、`/cyber_gf/*` 路徑行為。
2. **P1 設定與共用資料格式**：新增 `pipeline.yaml`、`common/`，現有程式暫不動。
3. **P2 vendor 路徑**：搬 `vendor_voice.py`、換臉、外掛路由；`/cyber_gf/*` 路徑保持不變（廠商與前端已依賴），另加新名稱別名。
4. **P3 emotion**：帶入早期分類器，做成 UDP 服務與用戶端；`vendor` 模式不載入。
5. **P4 full 模式**：拆現有 `voice_service.py` 為 intake／dialogue／voice，接上 BERT 平行與下游用戶端。
6. **P5 下游框架協定**：實作 `framed_client`／`framed_receiver`，取代 HTTP 分塊 POST。
7. **P6 ops 與文件**。

## 7. 已確認的決定

- 套件名稱 `imood`；命名與寫法用我們自己的習慣。
- BERT 鏈路用 UDP；其餘沿用早期協定。
- 繳交不需要開源元件清單與授權說明（POC 性質）；雲端由後續接手的人處理。
- 最終成品放在 `latest_imood_ai`，開分支，不進 `main`。

## 8. 已確認（補充）

- BERT 的原始版本在 WSL **Ubuntu-22.04** `/home/cgmhaha/imood-emotion`，`emotion/service.py` 是 HTTP API（`POST /emotion {"text"}` → `{"emotion": 0~4}`，預設埠 8102，無狀態）。最終版把它的鏈路改為 UDP，分類器與前處理沿用。
- full 模式要求「能跑通」，不要求速度或 VRAM 餘裕；**vendor 模式必須維持正常運作**（優先級高於 full）。
- `/cyber_gf/*` 路徑必須保留（廠商送端、前端頁面、語音服務與隊友的接收端都在用），新名稱只做別名。

## 9. 需要處理的細節

1. **標籤映射：沿用早期 `emotion/labels.py` 的 `NATIVE_TO_FIVE`，只在最後多加一層對到廠商代碼。**
   早期原生 8 類 → 5 類（0 中性／1 喜／2 怒／3 哀／4 樂）完全不改；再加 `FIVE_TO_VENDOR`：

   | 原生 8 類（模型） | 早期 5 類 | 廠商代碼 |
   |---|---|---|
   | 平淡語氣、疑問語調 | 中性 | neutral |
   | 開心語調 | 樂 | happy |
   | 驚奇語調 | 喜 | happy |
   | 悲傷語調、關切語調 | 哀 | sad |
   | 憤怒語調、厭惡語調 | 怒 | angry |
   | （模型沒有） | — | fear（只有廠商才會給） |

   喜、樂都對到 happy；fear 永遠不會由 BERT 產生。
   **層級：BERT 服務（UDP）對外回「早期 5 類 code（0–4）」**（8 類→5 類在服務內完成，與早期 HTTP 介面的 `emotion` 值相同）；5 類→廠商字串（`FIVE_TO_VENDOR`）在下游用戶端做，BEGIN 的 `emotion` 欄位帶廠商字串；換臉那層再把 angry／fear／未知收成 calm（現行行為不變）。預設：逾時／失敗／文字過短 → code 0 → neutral → calm。詳見 [architecture_compare.html](architecture/architecture_compare.html) C-6（BEGIN.emotion 型別等介面為待確認）。
2. **本機媒體接收端（:8030，`imood_media_receiver`）** 是另一位組員負責的 WebRTC 中繼，不在這次範圍，改寫時不動它。
