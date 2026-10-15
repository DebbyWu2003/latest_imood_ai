# 交接清單：廠商即時語音接收端

## 一、已完成（已在 `main`）

| 項目 | 位置 |
|---|---|
| 廠商即時語音接收端 | `scripts/wsl/vendor_voice.py`，路由 `ws://localhost:8010/cyber_gf/voice` |
| 模擬測試（15 項通過，不需顯示卡） | `scripts/wsl/vendor_voice_test.py` |
| 舊語音辨識、AI 回答、語音合成預設關閉 | 開關 `LEGACY_VOICE=0`，檔案都還在 |
| 接收端說明與測試結果 | [vendor_voice_receiver.md](vendor_voice_receiver.md) |
| 情緒介面規格 | [emotion_interface.md](emotion_interface.md) |

實機測試（廠商發送端 + 真的數字人）都通過：單段播放、連續多段不同情緒、中途停止、重啟後自動重連、閒置 10 分鐘、即時串流與一次傳送。

## 二、每個接手的人要做的設定

這兩個設定檔是**每個人自己電腦的設定**，不會放進 git，拉到程式後不會自動有，要自己建。

### 設定 1：廠商語音的密碼（一定要做）

廠商連進來時要帶一個密碼，接收端用它確認身分。密碼自己取，接收端和發送端填的一樣即可。

1. 打開 `config` 資料夾。
2. 複製 `paths.local.example.env`，檔名改成 `paths.local.env`（已有就直接打開）。
3. 加一行（`export` 要寫，`=` 前後不要有空格）：
   ```
   export VENDOR_VOICE_TOKEN=自己取的密碼
   ```

沒設的話，廠商的所有連線都會被拒絕（驗證失敗）。

### 設定 2：舊的語音流程（預設不用動）

舊的語音辨識、AI 回答、語音合成預設是關的。只有想開回來時才改，兩個檔案都要改：

- `config/paths.local.env` 加 `export LEGACY_VOICE=1`
- `config/paths.local.bat`（從 `paths.local.example.bat` 複製）加 `set LEGACY_VOICE=1`

### 啟動

執行 `scripts\windows\start_step12_v2.bat`。預設只開數字人，並打開 http://localhost:8010/imood/imood_ai.html。

### 用廠商發送端測試

1. 打開 imood 頁面，按「開始視訊」。
2. 打開廠商發送端 https://websocket-audio-tester.cielsi.workers.dev/sender。
3. 「連線目標」選「外部接收端」，網址填 `ws://localhost:8010/cyber_gf/voice`，Token 填上面設定的密碼，按「連線」。
4. 按範例音檔的「傳送」。

## 三、各人負責的事

| 誰 | 要做什麼 |
|---|---|
| 情緒同學 | 依 [emotion_interface.md](emotion_interface.md)，監聽 `/sse` 的 `emotion` 通知，切換表情；語音播完回到平靜 |
| 雲端同學 | 部署上雲端，提供 `wss://` 網址與憑證（只要把 `/cyber_gf/voice` 當 WebSocket 轉發），並把測試與正式環境的網址與 token 提供給廠商 |
| 專案負責人 | 向廠商確認下面第四點的問題 |

## 四、要問廠商的問題（尚無答案）

1. 使用者的問題（語音或文字）要送到哪個 API？有沒有規格文件？
2. 上線後，真正傳語音給我們的是他們哪個服務？現在的發送端網頁只是測試用的嗎？
3. 語音是不是已經想好的完整回答？（目前只是印象，廠商文件沒有寫。）

## 五、等廠商回覆後才能做

- **前端錄音：** 目前錄音鍵會顯示「語音服務無法使用」。取得廠商 API 後，改 `web/imood_ai.html` 的 `ask()` 函式，把送出網址換成廠商的，並拿掉輪詢辨識與回答進度的部分。
- **舊流程是否徹底刪除：** 目前只是停用，檔案都還在。

## 六、已知限制

- 廠商協定沒有 session 編號，語音會送到**最近開啟的視訊連線**；多人同時使用會有問題。
- 舊流程與新流程同時開（`LEGACY_VOICE=1`）時，聲音會排在同一個佇列，這種情況沒有測過。
- 情緒通知目前沒有任何程式在接收，格式只在模擬測試中驗證過。
- 廠商文件還沒提供：正式環境的 `wss://` 網址與憑證。
