"""Protocol test for vendor_voice.VendorReceiver with a fake LiveTalking session (no GPU needed).

Run (Ubuntu-24.04):  /root/livetalking/LiveTalking/.venv-lt/bin/python scripts/wsl/vendor_voice_test.py
"""
import asyncio, json, os, queue, sys, threading, uuid
from types import SimpleNamespace

import numpy as np
import websockets
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vendor_voice import VendorReceiver, FRAME_BYTES

TOKEN = "test-token"
ORIGIN = "https://websocket-audio-tester.cielsi.workers.dev"


class FakeSession:
    def __init__(self):
        self.asr = SimpleNamespace(queue=queue.Queue())
        self.msgqueues = []
        self.played = []                                    # (frame_count_before_event, userdata)
        threading.Thread(target=self._player, daemon=True).start()

    def add_msgqueue(self, q): self.msgqueues.append(q)

    def send_msg(self, msg):
        for q in list(self.msgqueues): q.put(msg)

    def _player(self):                                      # stands in for LiveTalking: consume + notify like BaseAvatar
        n = 0
        while True:
            it = self.asr.queue.get(); n += 1
            if it.userdata.get("status"):
                self.played.append((n, it.userdata)); self.send_msg(json.dumps(it.userdata))


def pcm(seconds, hz=440, amp=0.5):
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * hz * t) * amp * 32767).astype("<i2").tobytes()


def start_msg(uid, emotion="happy", **kw):
    m = {"type": "utterance.start", "version": 1, "utterance_id": uid, "emotion": emotion,
         "format": "pcm_s16le", "sample_rate": 16000, "channels": 1}
    m.update(kw)
    return json.dumps(m)


async def send_utt(ws, uid, raw, emotion="happy", aborted=False):
    await ws.send(start_msg(uid, emotion))
    chunks = [raw[i:i + 640] for i in range(0, len(raw), 640)]
    for c in chunks:
        await ws.send(c)
    await ws.send(json.dumps({"type": "utterance.end", "utterance_id": uid, "chunks": len(chunks), "aborted": aborted}))


async def recv_until(ws, pred, timeout=3):
    out = []
    async def go():
        while True:
            m = json.loads(await ws.recv()); out.append(m)
            if pred(out): return
    await asyncio.wait_for(go(), timeout)
    return out


async def main():
    sess = FakeSession()
    holder = {"sess": sess}
    rx = VendorReceiver(lambda: holder["sess"], lambda d, ud: SimpleNamespace(data=d, userdata=ud), token=TOKEN, origins={ORIGIN})
    app = web.Application(); app.router.add_get("/voice", rx.handle)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18765); await site.start()
    url = f"ws://127.0.0.1:18765/voice?token={TOKEN}"
    hdr = {"Origin": ORIGIN}
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name); ok &= bool(cond)

    # 1. wrong token / wrong origin -> 4001, and must not kick an existing sender
    async with websockets.connect(url, origin=ORIGIN) as good:
        for bad_url, bad_origin in ((url.replace(TOKEN, "nope"), ORIGIN), (url, "https://evil.example")):
            async with websockets.connect(bad_url, origin=bad_origin) as bad:
                try: await bad.recv()
                except websockets.ConnectionClosed as e: check(f"auth failure closes with 4001 ({bad_origin})", e.rcvd.code == 4001)
        await good.ping()
        check("failed auth does not kick the live sender", True)

        # 2. normal utterances: 3 s happy, then 1.3 s sad queued right behind it
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        raw_a, raw_b = pcm(3.0), pcm(1.3, 880)
        await send_utt(good, a, raw_a, "happy"); await send_utt(good, b, raw_b, "sad")
        msgs = await recv_until(good, lambda o: sum(m["type"] == "playback.ended" for m in o) == 2)
        seq = [(m["type"], m["utterance_id"]) for m in msgs]
        check("playback events in order a,b", seq == [("playback.started", a), ("playback.ended", a), ("playback.started", b), ("playback.ended", b)])
        frames_total = (len(raw_a) + len(raw_b)) // FRAME_BYTES
        check("all audio enqueued once (+2 end frames)", sess.played[-1][0] == frames_total + 2 and len(raw_a) % FRAME_BYTES == 0)
        check("emotions per utterance", [u["emotion"] for _, u in sess.played if u["status"] == "start"] == ["happy", "sad"])

        # 3. emotion SSE message: a listener on the session sees it when playback starts
        sse = queue.Queue(); sess.add_msgqueue(sse)
        c = str(uuid.uuid4())
        await send_utt(good, c, pcm(0.6), "angry")
        await recv_until(good, lambda o: any(m["type"] == "playback.ended" for m in o))
        emo = [json.loads(sse.get_nowait()) for _ in range(sse.qsize())]
        emo = [m for m in emo if m.get("type") == "emotion"]
        check("emotion message {type,utterance_id,emotion}", emo == [{"type": "emotion", "utterance_id": c, "emotion": "angry"}])

        # 4. unknown emotion -> neutral; short odd-length utterance is zero-padded
        d = str(uuid.uuid4()); n0 = len(sess.played)
        await send_utt(good, d, pcm(0.1)[:-100], "surprised")
        await recv_until(good, lambda o: any(m["type"] == "playback.ended" for m in o))
        check("unknown emotion -> neutral", sess.played[n0][1]["emotion"] == "neutral")

        # 5. unsupported format / bad uuid / no session -> error, nothing enqueued
        n_q = sess.asr.queue.qsize(); n_play = len(sess.played)
        await good.send(start_msg(str(uuid.uuid4()), sample_rate=44100)); await good.send(b"\0" * 640)
        e = json.loads(await asyncio.wait_for(good.recv(), 2))
        check("unsupported_format error", e["type"] == "error" and e["code"] == "unsupported_format")
        await good.send(start_msg("../../etc/passwd"))
        e = json.loads(await asyncio.wait_for(good.recv(), 2))
        check("invalid_utterance_id error", e["code"] == "invalid_utterance_id")
        holder["sess"] = None
        await good.send(start_msg(str(uuid.uuid4())))
        e = json.loads(await asyncio.wait_for(good.recv(), 2))
        check("no_session error", e["code"] == "no_session")
        holder["sess"] = sess
        await good.send("not json {")
        e = json.loads(await asyncio.wait_for(good.recv(), 2))
        check("bad_message error, connection survives", e["code"] == "bad_message")
        await asyncio.sleep(0.2)
        check("rejected utterances enqueue nothing", sess.asr.queue.qsize() == n_q and len(sess.played) == n_play)

        # 6. aborted utterance: partial audio is closed with an end frame
        f = str(uuid.uuid4()); n0 = len(sess.played)
        await send_utt(good, f, pcm(1.0), aborted=True)
        await recv_until(good, lambda o: any(m["type"] == "playback.ended" for m in o))
        check("aborted utterance still ends", sess.played[-1][1]["status"] == "end")

        # 7. new start before the previous end: previous one is closed, new one plays
        g, h = str(uuid.uuid4()), str(uuid.uuid4())
        await good.send(start_msg(g)); [await good.send(pcm(0.02)) for _ in range(40)]
        await send_utt(good, h, pcm(0.5), "fear")
        msgs = await recv_until(good, lambda o: sum(m["type"] == "playback.ended" for m in o) == 2)
        check("superseded utterance ended, then new one", [m["utterance_id"] for m in msgs if m["type"] == "playback.ended"] == [g, h])

        # 8. a second authenticated sender replaces the first (4002)
        async with websockets.connect(url, origin=ORIGIN):
            try:
                while True: await good.recv()
            except websockets.ConnectionClosed as e:
                check("old sender closed with 4002", e.rcvd.code == 4002)

    # 9. disconnect mid-utterance closes the utterance
    async with websockets.connect(url, origin=ORIGIN) as w:
        await w.send(start_msg(str(uuid.uuid4()))); [await w.send(pcm(0.02)) for _ in range(40)]
    await asyncio.sleep(0.5)
    check("disconnect mid-utterance emits end frame", sess.played[-1][1]["status"] == "end")

    await runner.cleanup()
    print("\nALL PASS" if ok else "\nSOME FAILED"); sys.exit(0 if ok else 1)


asyncio.run(main())
