"""Headless end-to-end check for Step 12 v2 (stand-in for the browser frontend).

Connects to LiveTalking over WebRTC exactly like web/imood_ai.html (POST /offer), starts LiveTalking's
recorder, submits a question WAV to the voice service (same call the page makes), and records what a
WebRTC viewer actually receives: 48 kHz audio + video frame arrival times.
Then reports audible gaps inside the spoken answer, received video fps, and the job metrics.
Run with the LiveTalking venv (has aiortc):  python streaming_e2e_test.py [--mode sentence|full] [--in wav]
"""
import argparse, asyncio, json, sys, time, urllib.request
from pathlib import Path
import numpy as np
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

ROOT = Path(__file__).resolve().parents[3]
LT, VS = "http://127.0.0.1:8010", "http://127.0.0.1:8020"


def gaps(pcm, sr, speech_start, speech_end, thr=0.004, min_gap=0.06):
    """Silent runs (20 ms RMS < thr) longer than min_gap between the first and last speech sample."""
    win = int(sr * 0.02)
    n = len(pcm) // win
    rms = np.sqrt((pcm[: n * win].reshape(n, win) ** 2).mean(1))
    t = np.arange(n) * 0.02
    voiced = np.where((rms > thr) & (t >= speech_start) & (t <= speech_end))[0]
    if len(voiced) == 0:
        return [], None
    a, b = voiced[0], voiced[-1]
    out, run = [], 0
    for i in range(a, b + 1):
        if rms[i] <= thr:
            run += 1
        else:
            if run * 0.02 >= min_gap:
                out.append((round((i - run) * 0.02, 2), round(run * 0.02, 2)))
            run = 0
    return out, (round(a * 0.02, 2), round(b * 0.02, 2))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="full")
    ap.add_argument("--in", dest="inp", default=str(ROOT / "assets/test.wav"))
    ap.add_argument("--tag", default="")
    ap.add_argument("--avatar", default="", help="per-session avatar (LiveTalking /offer option); empty = server default")
    args = ap.parse_args()

    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly"); pc.addTransceiver("audio", direction="recvonly")
    audio, vtimes, t0 = [], [], time.time()

    @pc.on("track")
    def on_track(track):
        async def pump():
            while True:
                try:
                    f = await track.recv()
                except Exception:
                    return
                if track.kind == "audio":
                    x = f.to_ndarray().astype(np.float32).flatten() / 32768.0
                    if f.layout.name == "stereo":
                        x = x.reshape(-1, 2).mean(1)
                    audio.append((time.time() - t0, x, f.sample_rate))
                else:
                    vtimes.append(time.time() - t0)
        asyncio.ensure_future(pump())

    await pc.setLocalDescription(await pc.createOffer())
    async with aiohttp.ClientSession() as s:
        ans = await (await s.post(f"{LT}/offer", json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, **({"avatar": args.avatar} if args.avatar else {})})).json()
        sid = ans["sessionid"]
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
        for _ in range(100):                                   # wait until media flows
            if len(vtimes) > 25: break
            await asyncio.sleep(0.1)
        await s.post(f"{LT}/record", json={"type": "start_record", "sessionid": sid})
        await asyncio.sleep(1.0)
        t_ask = time.time() - t0
        job = (await (await s.post(f"{VS}/api/ask?sessionid={sid}&mode={args.mode}", data=Path(args.inp).read_bytes())).json())["job"]
        while True:
            j = await (await s.get(f"{VS}/api/job/{job}")).json()
            if j["state"] in ("done", "error"):
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)
        await s.post(f"{LT}/record", json={"type": "end_record", "sessionid": sid})
        await asyncio.sleep(1.5)
        rec = await s.get(f"{LT}/record/{sid}")
        rec_path = ROOT / "outputs/step12_v2" / j["job"] / "livetalking_record.mp4"
        if rec.status == 200:
            rec_path.write_bytes(await rec.read())
    await pc.close()

    sr = audio[0][2] if audio else 48000
    pcm = np.concatenate([x for _, x, _ in audio]) if audio else np.zeros(1)
    first_audio_t = audio[0][0] if audio else 0
    ts = j["timestamps_s"]
    sp0 = t_ask - first_audio_t + ts.get("avatar_speaking_start", 0) - 0.2
    sp1 = t_ask - first_audio_t + ts.get("avatar_speaking_end", 0) + 0.2
    g, span = gaps(pcm, sr, sp0, sp1)
    import soundfile as sf
    out = ROOT / "outputs/step12_v2" / j["job"]
    sf.write(str(out / "webrtc_received_audio.wav"), pcm, sr)
    vt = np.array([t for t in vtimes if t_ask + ts.get("avatar_speaking_start", 0) <= t <= t_ask + ts.get("avatar_speaking_end", 1e9)])
    dv = np.diff(vt) if len(vt) > 2 else np.array([0])
    res = {"job": j["job"], "state": j["state"], "error": j["error"], "mode": args.mode, "sessionid": sid,
           "transcript": j["transcript"], "reply": j["reply"], "segments": j["segments"], "timestamps_s": ts, "metrics": j["metrics"],
           "webrtc_viewer": {"audio_gaps_over_60ms_in_answer": g, "voiced_span_s": span,
                             "video_fps_during_answer": round(len(vt) / max(vt[-1] - vt[0], 1e-6), 2) if len(vt) > 2 else None,
                             "video_max_interframe_ms": round(float(dv.max()) * 1000, 1), "video_frames_over_60ms": int((dv > 0.06).sum())},
           "recording": str(rec_path.relative_to(ROOT)) if rec_path.exists() else None}
    (out / "e2e_check.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))


asyncio.run(main())
