"""Emotion client: asks the BERT UDP service for a code, never raises and never blocks the voice path.

Usage (full mode): start(text) right when the reply text is available (background task);
at the first audio chunk call result(task) - it returns the finished code, or the default if not done yet.
"""
import asyncio
import itertools
import json

from imood.common.labels import DEFAULT_CODE

_seq = itertools.count(1)
MAX_REPLY = 4096


class _Once(asyncio.DatagramProtocol):
    def __init__(self, seq, fut):
        self.seq, self.fut = seq, fut

    def datagram_received(self, data, addr):
        try:
            r = json.loads(data.decode("utf-8"))
            if int(r["seq"]) != self.seq or self.fut.done():
                return                                   # stale / foreign reply
            code = int(r["code"])
            if 0 <= code <= 4:
                self.fut.set_result((code, float(r.get("confidence", 0.0))))
        except Exception:
            pass


class EmotionClient:
    def __init__(self, host="127.0.0.1", port=8102, timeout_ms=300, default=DEFAULT_CODE):
        self.addr, self.timeout, self.default = (host, port), timeout_ms / 1000.0, default

    async def classify(self, text):
        """-> (code, confidence). Any failure (timeout, bad reply, network) -> (default, 0.0)."""
        try:
            loop = asyncio.get_running_loop()
            seq, fut = next(_seq), loop.create_future()
            transport, _ = await loop.create_datagram_endpoint(lambda: _Once(seq, fut), remote_addr=self.addr)
            try:
                transport.sendto(json.dumps({"seq": seq, "text": text}).encode("utf-8"))
                return await asyncio.wait_for(fut, self.timeout)
            finally:
                transport.close()
        except Exception as exc:
            print(f"[emotion] classify failed, default {self.default}: {exc!r}", flush=True)
            return self.default, 0.0

    def start(self, text):
        return asyncio.ensure_future(self.classify(text))

    def result(self, task):
        """Finished result if ready, else the default. Never waits."""
        if task is not None and task.done() and not task.cancelled():
            try:
                return task.result()[0]
            except Exception:
                pass
        return self.default


class EmotionClientSync:
    """Blocking-socket twin of EmotionClient for the thread-based voice service: start(text) runs one request in a
    background thread; result() returns the finished code or the default - it never waits."""

    def __init__(self, host="127.0.0.1", port=8102, timeout_ms=300, default=DEFAULT_CODE):
        self.addr, self.timeout, self.default = (host, port), timeout_ms / 1000.0, default
        self._code, self._conf, self._done = default, 0.0, False

    def _run(self, text):
        import socket
        seq = next(_seq)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(self.timeout)
                s.sendto(json.dumps({"seq": seq, "text": text}).encode("utf-8"), self.addr)
                while True:                                   # skip stale replies from earlier requests
                    r = json.loads(s.recvfrom(MAX_REPLY)[0].decode("utf-8"))
                    if int(r["seq"]) == seq:
                        code = int(r["code"])
                        if 0 <= code <= 4:
                            self._code, self._conf = code, float(r.get("confidence", 0.0))
                        break
        except Exception as exc:
            print(f"[emotion] classify failed, default {self.default}: {exc!r}", flush=True)
        self._done = True

    def start(self, text):
        import threading
        threading.Thread(target=self._run, args=(text,), daemon=True).start()
        return self

    def result(self):
        """-> (code, ready). Not ready -> (default, False)."""
        return (self._code, True) if self._done else (self.default, False)
