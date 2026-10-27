"""Downstream client (service side): sends one utterance over the framed TCP link and reads STATUS back.

BEGIN {session, utterance, voice, sample_rate, emotion, text} -> AUDIO* (PCM16 LE mono 16 kHz) -> END.
STATUS frames come back on the same connection and are handed to on_status(dict).
"""
import json, socket, threading

import numpy as np

from imood.common import framing


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("downstream closed")
        buf += part
    return buf


class FramedClient:
    def __init__(self, host, port, timeout=10.0):
        self.host, self.port, self.timeout = host, int(port), timeout
        self.sock = None
        self.last_status = {}
        self._on_status = None
        self._reader = None
        self.done = threading.Event()          # set when the receiver reports the utterance finished (or the link drops)

    def open(self, on_status=None):
        self._on_status = on_status
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            while True:
                ftype, n = framing.unpack_header(_read_exact(self.sock, framing.HEADER.size))
                payload = _read_exact(self.sock, n) if n else b""
                if ftype == framing.STATUS:
                    st = json.loads(payload.decode("utf-8"))
                    self.last_status = st
                    if self._on_status:
                        self._on_status(st)
                    if st.get("state") in ("playback_ended", "error"):
                        break
        except Exception:
            pass
        finally:
            self.done.set()

    def begin(self, session, utterance, emotion, text="", voice="", sample_rate=16000):
        meta = {"session": session, "utterance": utterance, "voice": voice, "sample_rate": sample_rate,
                "emotion": emotion, "text": text}
        self.sock.sendall(framing.pack(framing.BEGIN, json.dumps(meta, ensure_ascii=False).encode("utf-8")))

    def audio_f32(self, b):
        """b = float32-LE bytes (what the TTS stage produces); sent as PCM16."""
        x = np.frombuffer(b, dtype="<f4")
        pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        self.sock.sendall(framing.pack(framing.AUDIO, pcm))

    def end(self):
        self.sock.sendall(framing.pack(framing.END))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
