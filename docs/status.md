# 進度（依文章步驟）

最後更新：2026-09-28（晚）。目前在執行：llama-server :8090。CosyVoice 和 :8000 服務暫停中（本機說明，不納入 Git）。

| 步驟 | 狀態 | 結果 |
|---|---|---|
| 1 `.wslconfig` | ✅ 已存在 | 內容和文章一樣（mirrored + hostAddressLoopback），WSL 已套用；WSL 可用 localhost 連到 Windows |
| 2 llama.cpp + Qwen3-14B | ✅ 單獨測試通過 | 首字 83–346 ms，約 88 tok/s，人設回覆正確，VRAM 約 10.4 GB |
| 3 數字人底圖 | ✅ | 文章原圖 1106×1422（`assets/avatar/`） |
| 4 ComfyUI Wan2.2 → `idle.mp4` | ✅ | 704×896、81 幀 @16fps、5.06 秒，生成 19.5 分鐘 |
| 5 `ref.wav` | ✅ | 你提供：`assets/ref.wav`（24 kHz、6.06 s）+ `config/ref_text.txt` |
| 6 部署包 | 不再等待 | 改用官方開源元件自己重建 |
| 7 WSL Ubuntu-24.04 | ✅ | 本來就有，GPU 直通正常 |
| 8 拷進 WSL + dos2unix | 部分 | dos2unix 已裝，還沒有檔案可拷 |
| 9 語音部分（自行重建） | ✅ 可跑通，TTS 太慢 | test.wav → Whisper → Qwen3 → Qwen3-TTS → reply.wav。ASR 0.84 s、LLM 首字再 0.13 s，但 TTS 即時率 3.3×，端到端 31–49 s。詳見 `step9_voice_test.md` |
| 10 LiveTalking | ✅ 測試通過 | inferfps 91–96 / finalfps 25（文章是 92 / 25） |
| 11 自訂形象 | ✅ 測試通過 | `wav2lip256_myavatar`，37 秒建好，說話時嘴會動 |
| 12 全部串起來（MVP，非串流） | ✅ 跑通，待你目視確認 | test.wav → Whisper → Qwen3 → faster-qwen3-tts → reply.wav → /humanaudio → wav2lip256_myavatar。輸入後 6.9 s 開始說話。見 `step12_mvp.md` |

## 測試證據
- `assets/test_frames/idle_tiles.jpg`：`idle.mp4` 第 1、41、81 幀
- `assets/test_frames/mouth_tiles.jpg`：自訂形象閒置和說話時的對照
- `assets/test_frames/official_avatar1_frame.jpg`：官方示例形象的輸出
- `logs/`：安裝和執行的 log

## 還沒解決的問題
1. **VRAM**：文章整套大約需要 17–18 GB。你的 CosyVoice（啟用時約 10 GB）和 Windows 8000 服務也在用 GPU，三者同時跑可能超過 24 GB。
2. **第四步的矛盾**：見 `deviations.md` 第 4 條。
3. 端到端延遲和總 VRAM 要等第十二步才能量測。

## Step 12 v2（串流版，2026-09-29）
✅ 常駐服務 + faster-qwen3-tts 串流 chunk 8 + 專案 adapter（LiveTalking 原始碼未修改）+ 瀏覽器 imood 前端頁。數字人開始說話平均 3.15 s（MVP 6.91 s），5/5 次 0 欠載、0 溢位，錄影同步。見 `step12_v2.md`。
啟動：`scripts\windows\start_step12_v2.bat` → http://localhost:8010/cyber_gf/web/cyber_gf_demo.html
