# 新前端 ↔ RTP 接收端 介面規格

新前端：`web/imood_ai.html`，網址 http://localhost:8010/imood/imood_ai.html。

## 架構

```
                       ┌──────────────────────────────┐
  瀏覽器               │ LiveTalking + adapter :8010  │
  imood_ai.html        │  /offer  /sse  /record       │
   │                   └──────────────▲───────────────┘
   │ ① POST {media}/offer            │ （接收端自己決定怎麼從 LiveTalking 拿 RTP）
   │    ← WebRTC 影像＋聲音           │
   ├──────────────► RTP 接收端 :8030
   │
   │ ② POST {voice}/api/ask?sessionid=…  ──►  voice service :8020（不變）
   │ ③ /sse、/record ──────────────────────►  LiveTalking :8010（不變）
```

前端把服務分成三個 base URL，每一個都能單獨切換：

| 名稱 | 用途 | 預設值 |
|---|---|---|
| `media` | WebRTC 協商（`POST /offer`） | 空字串 = 本頁同源（LiveTalking :8010） |
| `control` | LiveTalking 的 `/sse`、`/record` | 空字串 = 本頁同源 |
| `voice` | 語音服務 `/api/ask`、`/api/job/{id}` | `http://<本機>:8020` |

**新接收端只需要實作 `media` 這一個介面。** 另外兩個照舊直接連 LiveTalking 和語音服務。

## 接收端必須實作的介面

### `POST /offer`

和 LiveTalking 原本的 `/offer` 格式完全相同，所以前端程式不用改。

Request（`Content-Type: application/json`）：
```json
{ "sdp": "v=0\r\n...", "type": "offer", "avatar": "wav2lip256_avatar2_mA1" }
```
- `sdp`、`type`：瀏覽器產生的 offer。瀏覽器開了兩個 **recvonly** transceiver，順序是先 video、後 audio。
- `avatar`：選填，是 `web/avatars.json` 裡的 id。沒帶就用伺服器預設角色。接收端要把它轉給 LiveTalking。

成功 Response：
```json
{ "sdp": "v=0\r\n...", "type": "answer", "sessionid": "5b9f41fb-9d38-42ec-8110-bfb57b6903ed" }
```
失敗 Response：沒有 `sdp` 欄位，並在 `msg` 寫原因，例如 `{ "code": -1, "msg": "LiveTalking unreachable" }`。前端會把 `msg` 顯示出來。

### 規則
1. **`sessionid` 必須是 LiveTalking 真正的 session id**，而且要跟這條影像串流是同一個 session。前端會拿它去呼叫語音服務 `/api/ask?sessionid=…`（決定數字人要用哪個 session 開口），也會拿它呼叫 `/record`、`/sse`。id 錯了，畫面就不會對嘴。
2. **影像要 video、聲音要 audio**，兩條 track 都要送。前端用 `e.track.kind` 分流，聲音和嘴型的同步由 LiveTalking 負責，接收端不要另外重新對時。
3. **CORS**：前端頁面是從 :8010 開的，接收端在其他 port 屬於跨來源請求，必須：
   - 對 `OPTIONS /offer` 回 204，並帶 `Access-Control-Allow-Origin: *`、`Access-Control-Allow-Methods: POST, OPTIONS`、`Access-Control-Allow-Headers: Content-Type`
   - 對 `POST /offer` 的回應也要帶 `Access-Control-Allow-Origin: *`
   （或者接收端自己也提供這個 HTML 頁面，就不會有跨來源問題。）
4. **斷線清理**：瀏覽器按「結束視訊」只會關掉 RTCPeerConnection，不會另外打 API。接收端要在 peer connection 變成 `closed`／`failed` 時，自行釋放對應的 LiveTalking session。
5. **ICE**：前端預設不使用 STUN（本機或同網段直接連）。使用者在設定勾選 STUN 時，會用 `stun:stun.l.google.com:19302`。瀏覽器最多等 2 秒收集 ICE candidate，所以 answer 裡要帶上接收端的 candidate（non-trickle，跟 LiveTalking 一樣）。

## 怎麼切換到新接收端

不用改程式，下面兩種方式擇一：
- 網址加參數：`http://localhost:8010/imood/imood_ai.html?media=http://localhost:8030`
- 頁面右上角 ⚙ 設定 →「影像來源」填 `http://localhost:8030` → 套用（存在瀏覽器裡，下次開還在）

其他參數：`?control=…`、`?voice=…`、`?stun=1`。

## 自我測試清單（接收端）

```bash
# CORS preflight
curl -i -X OPTIONS http://localhost:8030/offer -H "Origin: http://localhost:8010" -H "Access-Control-Request-Method: POST"
```
1. 用 `?media=http://localhost:8030` 開新前端，按「開始視訊」，畫面出現、右上角顯示「視訊中」。
2. ⚡ 診斷 → WebRTC 接收：影像 fps 大約 25。
3. 送出 `assets/test.wav`：數字人用複製的聲音回答，而且對嘴。這一步確認 `sessionid` 正確。
4. 結束視訊後，`curl http://localhost:8010/cyber_gf/stats` 的 `sessions` 不應留下這個 session。
