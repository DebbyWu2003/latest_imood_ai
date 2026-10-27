"""Pure-python checks of the common layer. Run: python -m imood.tests.test_common (repo root)."""
from imood.common import config, framing, labels


def test_labels():
    assert labels.native_to_code("開心語調") == 4 and labels.code_to_vendor(4) == "happy"
    assert labels.code_to_vendor(1) == "happy"            # 喜 and 樂 both -> happy
    assert labels.code_to_vendor(3) == "sad" and labels.code_to_vendor(2) == "angry"
    assert labels.code_to_vendor(99) == "neutral" and labels.native_to_code("?") == 0
    assert labels.vendor_to_face("angry") == "calm" and labels.vendor_to_face("fear") == "calm"
    assert labels.vendor_to_face("happy") == "happy" and labels.vendor_to_face("zzz") == "calm"
    assert set(labels.NATIVE_TO_FIVE) == {"平淡語氣", "關切語調", "開心語調", "憤怒語調", "悲傷語調", "疑問語調", "驚奇語調", "厭惡語調"}


def test_framing():
    f = framing.pack(framing.AUDIO, b"\x01\x02")
    assert framing.unpack_header(f[:5]) == (framing.AUDIO, 2) and f[5:] == b"\x01\x02"
    try:
        framing.unpack_header(b"\x09\x00\x00\x00\x00")
        raise SystemExit("bad type accepted")
    except ValueError:
        pass


def test_config():
    c = config.load()
    assert c["mode"] == "vendor" and c["vendor"]["ws_path"] == "/cyber_gf/voice"


if __name__ == "__main__":
    test_labels(); test_framing(); test_config()
    print("common: ALL PASS")
