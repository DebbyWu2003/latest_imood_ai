"""Loopback test of the framed link (no LiveTalking, no GPU): client -> receiver (fake ingest) -> STATUS back."""
import asyncio, threading, time

import numpy as np

from imood.downstream import framed_receiver
from imood.downstream.framed_client import FramedClient

GOT = {}


async def fake_ingest(req):
    pcm = b""
    async for b in req.content.iter_any():
        pcm += b
    GOT["query"], GOT["samples"] = dict(req.query), np.frombuffer(pcm, dtype="<f4")
    return None


def main():
    loop = asyncio.new_event_loop()
    stats = {}
    handler = framed_receiver.make_handler(fake_ingest, lambda sid: ("s1", object()),
                                           lambda sess, emo: GOT.__setitem__("emotion", emo), lambda u: stats.get(u))
    srv = loop.run_until_complete(asyncio.start_server(handler, "127.0.0.1", 0))
    port = srv.sockets[0].getsockname()[1]
    threading.Thread(target=loop.run_forever, daemon=True).start()

    states = []
    cl = FramedClient("127.0.0.1", port)
    cl.open(on_status=lambda st: states.append(st["state"]))
    cl.begin("s1", "u1", "happy", text="hi")
    x = (np.sin(np.arange(1600) / 10.0) * 0.5).astype("<f4")
    cl.audio_f32(x.tobytes()); cl.audio_f32(x.tobytes())
    cl.end()
    time.sleep(0.3)
    stats["u1"] = {"playback_start": 1.0}
    time.sleep(0.2)
    stats["u1"]["playback_end"] = 2.0
    assert cl.done.wait(5), "no playback_ended STATUS"
    cl.close()

    assert GOT["emotion"] == "happy", GOT
    assert GOT["query"] == {"sessionid": "s1", "utt": "u1"}, GOT["query"]
    assert len(GOT["samples"]) == 3200 and np.max(np.abs(GOT["samples"][:1600] - x)) < 1e-3
    assert states == ["received", "playback_started", "playback_ended"], states
    print("ALL PASS")


if __name__ == "__main__":
    main()
