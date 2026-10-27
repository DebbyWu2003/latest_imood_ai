"""Microphone path test (no GPU): Coalescer rules, Endpointer on a real speech wav, and the /ws/audio round trip with a fake ASR.

Run in the voice venv (needs webrtcvad, fastapi, soundfile, soxr):  python -m imood.tests.test_mic
"""
import time
from pathlib import Path

import numpy as np

from imood.intake.coalescer import Coalescer
from imood.intake.mic_endpointer import EndpointConfig, Endpointer

WAV = Path(__file__).resolve().parents[2] / "assets" / "開心測試音檔.wav"
CHUNK = 5120   # 160 ms of PCM16 @ 16 kHz


def speech_pcm():
    import soundfile as sf, soxr
    x, sr = sf.read(str(WAV), dtype="float32", always_2d=True)
    x = soxr.resample(x.mean(axis=1), sr, 16000)
    lead = (np.random.default_rng(0).normal(0, 20, 16000)).astype("<i2").tobytes()   # 1 s room noise: the VAD calibrates on it
    return lead + (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def test_coalescer():
    c = Coalescer(1000, 3)
    assert c.add("你好") is None and c.pending == ["你好"]
    assert c.due(True, 5000) is None, "still speaking"
    assert c.due(False, 500) is None, "silence too short"
    c.add("今天")
    assert c.due(False, 1000) == "你好今天"
    assert c.pending == []
    c.add("a"); c.add("b")
    assert c.add("c") == "abc", "max segments forces a flush"
    assert Coalescer(0).add("x") == "x", "coalesce_ms 0 = no waiting"


def test_endpointer():
    pcm = speech_pcm()
    ep = Endpointer(EndpointConfig())
    silence = bytes(CHUNK)
    out = []
    for i in range(0, len(pcm), CHUNK):
        u = ep.feed(pcm[i:i + CHUNK])
        if u:
            out.append(u)
    for _ in range(15):                       # 2.4 s of silence closes the last sentence
        u = ep.feed(silence)
        if u:
            out.append(u)
    assert out, "no utterance from a real speech recording"
    assert all(u.voiced_ms >= 400 for u in out)
    assert sum(len(u.pcm) for u in out) > len(pcm) * 0.5
    print(f"  {len(out)} utterance(s), {[round(u.duration_ms) for u in out]} ms")
    quiet = Endpointer(EndpointConfig())
    assert all(quiet.feed(silence) is None for _ in range(40)), "silence must not trigger"


def test_ws():
    from fastapi.testclient import TestClient
    from imood.intake import mic_ws
    jobs = []
    mic_ws._ENV.update(submit=lambda text, wav, sid: jobs.append((text, len(wav), sid)) or f"job{len(jobs)}",
                       asr_model=None, asr_cfg=None)
    mic_ws._asr = lambda pcm: "這是測試句子"
    pcm = speech_pcm()
    with TestClient(mic_ws.app).websocket_connect("/ws/audio?session=abc") as ws:
        sent = 0
        for i in range(0, len(pcm), CHUNK):
            ws.send_bytes(pcm[i:i + CHUNK]); sent += 1
        for _ in range(20):
            ws.send_bytes(bytes(CHUNK)); sent += 1
        types, t0 = [], time.time()
        while time.time() - t0 < 10 and "job" not in types:
            m = ws.receive_json(); types.append(m["type"])
            if m["type"] == "job":
                assert m["text"].startswith("這是測試句子")
    assert "asr_start" in types and "transcript" in types and "job" in types, types
    assert jobs and jobs[0][2] == "abc", jobs


if __name__ == "__main__":
    test_coalescer(); print("coalescer ok")
    test_endpointer(); print("endpointer ok")
    test_ws(); print("ws ok")
    print("ALL PASS")
