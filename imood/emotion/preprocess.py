"""前處理：把上游送來的文字整理成「可以送進分類器」的樣子。

pipeline 上的位置：模擬上游／STT → **前處理** → BERT 推論 → 輸出封包。

只做兩件事：
  1. 文字層過濾 —— 擋掉會讓模型「高信心答錯」的垃圾輸入
  2. 簡繁轉換   —— 只有語音辨識來的文字需要，見 prepare() 的說明
"""
import re


#最小可推論字數
#如果字數太少無法代表完整句意，也容易導致高信心的錯誤分類。
#目前MIN_CHARS
MIN_CHARS = 4

# 匹配確認整句不會只有標點符號
_PUNCT = "，。！？；：、「」『』（）〈〉《》…—～·．,.!?;:\"'()\\[\\]{}<>~`@#$%^&*_+=|/\\-"
_PUNCT_ONLY = re.compile(rf"^[\s{_PUNCT}]*$")

# faster-whisper 對中文靜音段的常見幻覺。
# 幻覺預處理清單，可排除幻覺句子進入推論
HALLUCINATIONS = {
    "謝謝觀看", "謝謝收看", "請不吝點贊訂閱轉發打賞支持明鏡與點點欄目",
    "字幕由Amara.org社群提供", "字幕志願者", "中文字幕由", "請訂閱",
    "谢谢观看", "谢谢收看", "请订阅",
    "以下是繁體中文的對話內容，請以繁體中文轉寫。",
    "請以繁體中文轉寫。", "以下是繁體中文的對話內容",
}

# OpenCC 的轉換器建一次就好（建立要讀字典檔，每句重建會很慢）。
_converter = None

# 簡體轉繁體用

def to_traditional(text: str) -> str:
    """簡體 → 繁體（台灣用字）。
    用 s2twp 不用 s2t
    s2t 只換字形，s2twp 連詞彙也換（軟件→軟體）。
    """
    global _converter
    if _converter is None:
        from opencc import OpenCC          # imported lazily: only needed when traditional=True
        _converter = OpenCC("s2twp")
    return _converter.convert(text)

#判斷句子是否通過且記錄原因
def clean(text: str) -> tuple[str, str]:
    """回傳 (整理後的文字, 被擋下來的原因)。
    
    第二格如果是空字串代表通過，非空則不通且記錄原因
    """
    text = text.strip()
    if not text:
        return "", "空白"
    if _PUNCT_ONLY.match(text):
        return "", "只有標點"
    if text in HALLUCINATIONS:
        return "", "語音辨識靜音幻覺"
    if len(text) < MIN_CHARS:
        return "", f"過短（{len(text)} 字，門檻 {MIN_CHARS}）"
    return text, ""

#前處理路口
def prepare(text: str, traditional: bool = False) -> tuple[str, str]:
    """前處理的單一入口。回傳 (整理後的文字, 被擋下來的原因)。

    traditional 預設 False，如果需要轉繁體再打開
    文字層過濾都跑
    """
    if traditional:
        text = to_traditional(text)
    return clean(text)
