# 設定與啟動:廠商即時語音接收端

廠商即時語音接收端的說明在 [vendor_voice_receiver.md](vendor_voice_receiver.md),情緒介面規格在 [emotion_interface.md](emotion_interface.md)。

## 一、前置設定

這兩個設定檔是**每個人自己電腦的設定**,不會放進 git,拉到程式後不會自動有,要自己建。

### 設定 1:廠商語音的密碼(一定要做)

廠商連進來時要帶一個密碼,接收端用它確認身分。密碼自己取,接收端和發送端填的一樣即可。

1. 打開 `config` 資料夾。
2. 複製 `paths.local.example.env`,檔名改成 `paths.local.env`(已有就直接打開)。
3. 加一行(`export` 要寫,`=` 前後不要有空格):
   ```
   export VENDOR_VOICE_TOKEN=自己取的密碼
   ```

沒設的話,廠商的所有連線都會被拒絕(驗證失敗)。

### 設定 2:本機語音流程(預設不用動)

本機的語音辨識、AI 回答、語音合成(Whisper + llama.cpp + TTS)預設是關的。只有想啟用時才改,兩個檔案都要改:

- `config/paths.local.env` 加 `export LEGACY_VOICE=1`
- `config/paths.local.bat`(從 `paths.local.example.bat` 複製)加 `set LEGACY_VOICE=1`

兩種設定的差別:

| | `LEGACY_VOICE=0`(預設) | `LEGACY_VOICE=1` |
|---|---|---|
| 啟動的服務 | 只有 LiveTalking(:8010) | LiveTalking(:8010)＋ llama-server(:8090)＋ 語音服務(:8020) |
| 廠商語音(`/cyber_gf/voice`) | 可用 | 可用 |
| 網頁麥克風、上傳 WAV | 不能用(沒有語音服務,會顯示「語音服務無法使用」) | 可用(辨識 → AI 回答 → 語音合成 → 數字人) |
| 顯示卡記憶體 | 較少 | 約 23 / 24 GB,很吃緊 |
| 與廠商語音同時使用 | 不適用 | 聲音排同一個佇列(此情況未驗證) |

只測廠商語音就用 `0`;要用網頁麥克風或本機語音流程才用 `1`。網頁的錄音鍵只在 `LEGACY_VOICE=1` 時可用。

## 二、啟動

執行 `scripts\windows\start_avatar_services.bat`。預設只開數字人,並打開 http://localhost:8010/imood/imood_ai.html。

## 三、用廠商發送端測試

1. 打開 imood 頁面,按「開始視訊」。
2. 打開廠商發送端 https://websocket-audio-tester.cielsi.workers.dev/sender。
3. 「連線目標」選「外部接收端」,網址填 `ws://localhost:8010/cyber_gf/voice`,Token 填上面設定的密碼,按「連線」。
4. 按範例音檔的「傳送」。

## 四、已知限制

- 廠商協定沒有 session 編號,語音會送到**最近開啟的視訊連線**;多人同時使用會有問題。
- `LEGACY_VOICE=1` 時,本機語音流程與廠商語音的聲音排在同一個佇列,這種情況未驗證。
- 情緒通知(`/sse`)沒有內建的接收端,格式只由協定測試驗證。
- 正式環境需要公開的 `wss://` 網址與有效憑證,不在本專案內提供。
