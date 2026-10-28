# 情緒介面規格（語音接收端 → 情緒處理）

語音接收端負責接收廠商的即時語音與情緒；情緒表現由情緒處理端負責。本文件定義兩者之間的交接方式。

## 一句話說明

每段語音**開始播放的那一刻**，語音接收端會透過 `/sse` 送出一則 `emotion` 通知。情緒處理端聽這個通知，依 `emotion` 切換數字人的表情。

## 通知格式

```json
{ "type": "emotion", "utterance_id": "3f2b8c1e-5a7d-4e9a-b1c2-0d4e6f8a9b3c", "emotion": "happy" }
```

| 欄位 | 型別 | 說明 |
|---|---|---|
| `type` | string | 固定為 `"emotion"`，用來和其他 `/sse` 訊息區分 |
| `utterance_id` | string | 這段語音的編號（UUID），來自廠商的 `utterance.start` |
| `emotion` | string | 情緒代碼，見下表 |

## 情緒代碼

| 代碼 | 名稱 |
|---|---|
| `neutral` | 平靜 |
| `happy` | 開心 |
| `sad` | 難過 |
| `angry` | 生氣 |
| `fear` | 害怕 |

- 代碼來自廠商規格。廠商日後可能新增（例如 `surprised`）。
- **收到不認識的代碼，一律當作 `neutral`**，不要報錯。
- 廠商沒帶情緒時，語音接收端會送 `neutral`。

## 來源與時機

- **來源**：LiveTalking 的 `/sse?sessionid=<sessionid>`（前端已在使用，網址同 `control` base URL，預設 `http://localhost:8010`）。
- **時機**：該段語音**實際開始播放**時才送，不是接收到的時候。語音可能排隊，第二段等待時，臉不應該提前變成第二段的情緒。
- 一段語音只送一次 `emotion`，情緒適用整段語音。

## 結束後的處理（建議約定）

廠商沒有規定一段語音結束後表情要如何處理。建議：**語音播完後回到 `neutral`**，避免表情停在生氣或難過。

語音播完的時機，可以聽 LiveTalking 既有的播放結束事件（`status` 為 `end`）。如果之後需要，語音接收端也可以另外送 `{ "type": "emotion_end", "utterance_id": "..." }`，請提出需求。

## 情緒處理端要做的事

1. 連線到 `/sse?sessionid=<sessionid>`。
2. 收到訊息時，先判斷 `type` 是否為 `"emotion"`，不是就略過。
3. 依 `emotion` 切換表情；不認識的代碼視為 `neutral`。
4. 語音播完後回到 `neutral`。

## 測試方式

語音接收端完成前，可以手動模擬：自行產生上述格式的 JSON，餵給情緒處理端，確認五種情緒都能正確切換，且不認識的代碼會落到 `neutral`。

語音接收端完成後，用廠商的發送端（https://websocket-audio-tester.cielsi.workers.dev/sender）連續送出不同情緒的語音，確認表情與聲音同步。

