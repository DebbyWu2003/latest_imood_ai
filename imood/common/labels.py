"""Emotion label layers (single source of truth).

native 8 classes (model) -> five classes (code 0-4)
                         -> vendor string (BEGIN.emotion / vendor protocol)
                         -> face (avatar variant).
"""
NATIVE_TO_FIVE = {
    "平淡語氣": 0, "疑問語調": 0,          # 中性
    "驚奇語調": 1,                          # 喜
    "憤怒語調": 2, "厭惡語調": 2,          # 怒
    "悲傷語調": 3, "關切語調": 3,          # 哀
    "開心語調": 4,                          # 樂
}
FIVE_LABELS = ["中性", "喜", "怒", "哀", "樂"]
FIVE_TO_VENDOR = {0: "neutral", 1: "happy", 2: "angry", 3: "sad", 4: "happy"}
VENDOR_EMOTIONS = ("neutral", "happy", "sad", "angry", "fear")
VENDOR_TO_FACE = {"neutral": "calm", "happy": "happy", "sad": "sad", "angry": "calm", "fear": "calm"}
DEFAULT_CODE = 0


def native_to_code(label):
    return NATIVE_TO_FIVE.get(label, DEFAULT_CODE)


def code_to_vendor(code):
    """Any unknown / out-of-range code falls back to the default (neutral)."""
    return FIVE_TO_VENDOR.get(code, FIVE_TO_VENDOR[DEFAULT_CODE])


def vendor_to_face(emotion):
    return VENDOR_TO_FACE.get(emotion, "calm")
