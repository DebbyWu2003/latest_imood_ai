"""BERT emotion service over UDP.

request  datagram: JSON {"seq": int, "text": str}
reply    datagram: JSON {"seq": int, "code": 0-4, "confidence": float}
code = five-class code (0 neutral, 1 joy, 2 anger, 3 sadness, 4 happy). Anything that cannot be
classified (blocked by preprocessing, model not ready, error) replies the default code 0, never an error.

Run (repo root):  python -m imood.emotion.udp_service      (env EMOTION_UDP_HOST / EMOTION_UDP_PORT / EMOTION_DEVICE / EMOTION_DTYPE)
"""
import asyncio
import json
import os

from imood.common import config as cfgmod
from imood.common.labels import DEFAULT_CODE, native_to_code
from imood.emotion.preprocess import prepare

MAX_DATAGRAM = 8192


class UdpEmotionService(asyncio.DatagramProtocol):
    def __init__(self, predict, loop=None):
        """predict(text) -> (code, confidence); blocking, runs in a worker thread."""
        self.predict = predict
        self.loop = loop or asyncio.get_event_loop()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.loop.create_task(self._handle(data, addr))

    async def _handle(self, data, addr):
        try:
            req = json.loads(data.decode("utf-8"))
            seq, text = int(req["seq"]), str(req["text"])
        except Exception:
            return                                   # unparsable: no seq to answer, the client times out and uses its default
        code, conf = DEFAULT_CODE, 0.0
        try:
            clean, blocked = prepare(text)
            if blocked:
                print(f"[emotion] blocked ({blocked}): {text!r}", flush=True)
            else:
                code, conf = await self.loop.run_in_executor(None, self.predict, clean)
        except Exception as exc:
            print(f"[emotion] predict failed, default code: {exc}", flush=True)
        self.transport.sendto(json.dumps({"seq": seq, "code": code, "confidence": conf}).encode("utf-8"), addr)


def model_predict(device="auto", dtype="fp16"):
    """Load the classifier once (+ warmup) and return predict(text) -> (code, confidence)."""
    from imood.emotion.classifier import EmotionClassifier, resolve_device
    dev = resolve_device(device)
    if not dev.startswith("cuda") and dtype == "fp16":
        dtype = "fp32"
    clf = EmotionClassifier(device=dev, dtype=dtype)
    clf.load()
    clf.predict("今天天氣不錯")

    def predict(text):
        p = clf.predict(text)
        return native_to_code(p.label), float(p.confidence)
    return predict


async def serve(predict, host, port):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: UdpEmotionService(predict, loop), local_addr=(host, port))
    print(f"[emotion] udp service on {host}:{port}", flush=True)
    return transport


def main():
    c = cfgmod.load().get("emotion", {})
    host = os.environ.get("EMOTION_UDP_HOST", "0.0.0.0")
    port = int(os.environ.get("EMOTION_UDP_PORT", c.get("udp_port", 8102)))
    predict = model_predict(os.environ.get("EMOTION_DEVICE", "auto"), os.environ.get("EMOTION_DTYPE", "fp16"))

    async def run():
        await serve(predict, host, port)
        await asyncio.Event().wait()
    asyncio.run(run())


if __name__ == "__main__":
    main()
