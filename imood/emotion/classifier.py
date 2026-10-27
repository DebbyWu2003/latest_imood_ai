# 2026-09-09 更新：load() 新增 dtype 參數（fp16 直接載，不再載完再 .half()），待GINO複核
"""模型載入、標籤核對、單句推論與計時。"""
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from .labels import MODEL_REVISION, NATIVE_LABELS, NATIVE_LABELS_SOURCE

#模型選擇
MODEL_ID = "Johnson8187/Chinese-Emotion-Small"
MAX_LENGTH = 160


@dataclass
class Prediction:
    label: str           # 情緒標籤
    confidence: float    # 信心度
    probs: dict          # 完整 8 類機率
    latency_ms: dict     # 各段耗時

#選定裝置
def resolve_device(requested: str) -> str:
    """把 CLI 的 --device 換成實際要用的裝置。
    指定GPU時為強制選用 不降級
    """
    available = torch.cuda.is_available()

    if requested == "cuda":
        if not available:
            raise SystemExit(
                "指定了 --device cuda，但 torch.cuda.is_available() 為 False。\n"
                "  不自動降級成 CPU\n"
                "  請改用掛了 GPU 的服務（compose 裡的 app，不是 app-cpu），"
                "或明確指定 --device cpu / --device auto。"
            )
        return "cuda"

    if requested == "auto":
        chosen = "cuda" if available else "cpu"
        print(f"--device auto → 實際使用 {chosen}"
              + ("" if available else "（未偵測到可用的 GPU）"))
        return chosen

    return "cpu"

#情緒分類器
class EmotionClassifier:
    def __init__(self, device: str, model_id: str = MODEL_ID, max_length: int = MAX_LENGTH,
                 dtype: str = "fp32"):
        self.device = device
        self.model_id = model_id
        self.max_length = max_length
        self.dtype = dtype
        self.tokenizer = None
        self.model = None
        self.load_seconds = None

    #載入模型
    def load(self) -> None:
        t0 = time.perf_counter()
        try:
            #固定版本
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_id, revision=MODEL_REVISION)
            # fp16 在 from_pretrained 就指定，不要載完 fp32 再 .half()
            # （.half() 要先把 fp32 權重整份搬到 GPU 再轉，H2D 傳輸量多一倍）
            # 參數名是 dtype 不是 torch_dtype（後者在新版 transformers 已 deprecated）
            self.model = AutoModelForSequenceClassification.from_pretrained(
                self.model_id, revision=MODEL_REVISION,
                dtype=torch.float16 if self.dtype == "fp16" else None)
        except Exception as exc:
            # 載入失敗最常見的原因
            raise SystemExit(
                f"模型載入失敗：{type(exc).__name__}: {exc}\n"
                f"  HF_HOME  = {os.environ.get('HF_HOME', '（未設定）')}\n"
                f"  .env 存在 = {Path('.env').exists()}\n"
                "  模型為公開資源，一般不需 token；若為網路問題請確認容器可連外。"
            )

        self.model.to(self.device)
        self.model.eval()  # 關掉 dropout，否則同一句每次跑出來的分數都不一樣
        if self.device.startswith("cuda"):
            torch.cuda.synchronize()
        #讀取時間
        self.load_seconds = time.perf_counter() - t0

        self._assert_labels()

    #核對標籤
    def _assert_labels(self) -> None:
        """比對模型與本地labels.py的標籤是否相符
            此函式僅核對標籤數量是否相同，無驗證順序
        """
        id2label = {int(k): v for k, v in self.model.config.id2label.items()}
        actual = [id2label[i] for i in sorted(id2label)]

        if all(re.fullmatch(r"LABEL_\d+", str(v)) for v in actual):
            if len(actual) != len(NATIVE_LABELS):
                raise SystemExit(
                    f"模型 {self.model_id} 的類別數與設定不符，中止。\n"
                    f"  設定 : {len(NATIVE_LABELS)} 類 {NATIVE_LABELS}\n"
                    f"  模型 : {len(actual)} 類\n"
                )
            print(f"  ⚠️ 模型 config 只有 LABEL_0..{len(actual) - 1} 佔位符，"
                  f"標籤順序無法從模型驗證，只驗到類別數 {len(actual)} 相符")
            print(f"     順序出處：{NATIVE_LABELS_SOURCE}")
            return

        if actual != NATIVE_LABELS:
            raise SystemExit(
                f"模型 {self.model_id} 的標籤與設定不符，中止。\n"
                f"  設定 : {NATIVE_LABELS}\n"
                f"  模型 : {actual}\n"
                "請更新 emotion/labels.py。"
            )
        print(f"  標籤核對通過：模型 config 的 {len(actual)} 類與設定一致")


    def predict(self, text: str) -> Prediction:
        t0 = time.perf_counter()
        #tokenize
        enc = self.tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_length,
        )
        t1 = time.perf_counter()

        #forward
        enc = {k: v.to(self.device) for k, v in enc.items()}
        with torch.inference_mode():
            logits = self.model(**enc).logits
        if self.device.startswith("cuda"):
            torch.cuda.synchronize()
        t2 = time.perf_counter()

        #後處理
        probs = torch.softmax(logits, dim=-1)[0].tolist()
        raw = {label: round(p, 6) for label, p in zip(NATIVE_LABELS, probs)}
        best = max(raw, key=raw.get)
        t3 = time.perf_counter()

        return Prediction(
            label=best,
            confidence=raw[best],
            probs=raw,
            latency_ms={
                "tokenize": round((t1 - t0) * 1000, 3),
                "forward": round((t2 - t1) * 1000, 3),
                "post": round((t3 - t2) * 1000, 3),
                "total": round((t3 - t0) * 1000, 3),
            },
        )
