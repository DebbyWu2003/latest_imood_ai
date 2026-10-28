"""Headless end-to-end check for Step 12 v2 (stand-in for the browser frontend).

Connects to LiveTalking over WebRTC exactly like web/imood_ai.html (POST /offer), starts LiveTalking's
recorder, submits a question WAV to the voice service (same call the page makes), and records what a
WebRTC viewer actually receives: 48 kHz audio + video frame arrival times.
Then reports audible gaps inside the spoken answer, received video fps, and the job metrics.
Run with the LiveTalking venv (has aiortc):  python streaming_e2e_test.py [--mode sentence|full] [--in wav]
"""
import argparse, asyncio, json, time
from pathlib import Path
import numpy as np
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

ROOT = Path(__file__).resolve().parents[3]
LIVETALKING_URL, VOICE_SERVICE_URL = "http://127.0.0.1:8010", "http://127.0.0.1:8020"


def gaps(pcm, sample_rate, speech_start, speech_end, threshold=0.004, min_gap=0.06):
    """Silent runs (20 ms RMS < thr) longer than min_gap between the first and last speech sample."""
    window = int(sample_rate * 0.02)
    window_count = len(pcm) // window
    rms = np.sqrt((pcm[: window_count * window].reshape(window_count, window) ** 2).mean(1))
    times = np.arange(window_count) * 0.02
    voiced = np.where((rms > threshold) & (times >= speech_start) & (times <= speech_end))[0]
    if len(voiced) == 0:
        return [], None
    first_voiced, last_voiced = voiced[0], voiced[-1]
    silent_runs, silent_count = [], 0
    for i in range(first_voiced, last_voiced + 1):
        if rms[i] <= threshold:
            silent_count += 1
        else:
            if silent_count * 0.02 >= min_gap:
                silent_runs.append((round((i - silent_count) * 0.02, 2), round(silent_count * 0.02, 2)))
            silent_count = 0
    return silent_runs, (round(first_voiced * 0.02, 2), round(last_voiced * 0.02, 2))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="full")
    parser.add_argument("--in", dest="inp", default=str(ROOT / "assets/test.wav"))
    parser.add_argument("--tag", default="")
    parser.add_argument("--avatar", default="", help="per-session avatar (LiveTalking /offer option); empty = server default")
    args = parser.parse_args()

    peer_connection = RTCPeerConnection()
    peer_connection.addTransceiver("video", direction="recvonly"); peer_connection.addTransceiver("audio", direction="recvonly")
    audio, video_times, started = [], [], time.time()

    @peer_connection.on("track")
    def on_track(track):
        async def pump():
            while True:
                try:
                    frame = await track.recv()
                except Exception:
                    return
                if track.kind == "audio":
                    samples = frame.to_ndarray().astype(np.float32).flatten() / 32768.0
                    if frame.layout.name == "stereo":
                        samples = samples.reshape(-1, 2).mean(1)
                    audio.append((time.time() - started, samples, frame.sample_rate))
                else:
                    video_times.append(time.time() - started)
        asyncio.ensure_future(pump())

    await peer_connection.setLocalDescription(await peer_connection.createOffer())
    async with aiohttp.ClientSession() as http_session:
        answer = await (await http_session.post(f"{LIVETALKING_URL}/offer", json={"sdp": peer_connection.localDescription.sdp, "type": peer_connection.localDescription.type, **({"avatar": args.avatar} if args.avatar else {})})).json()
        session_id = answer["sessionid"]
        await peer_connection.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        for _ in range(100):                                   # wait until media flows
            if len(video_times) > 25: break
            await asyncio.sleep(0.1)
        await http_session.post(f"{LIVETALKING_URL}/record", json={"type": "start_record", "sessionid": session_id})
        await asyncio.sleep(1.0)
        t_ask = time.time() - started
        job = (await (await http_session.post(f"{VOICE_SERVICE_URL}/api/ask?sessionid={session_id}&mode={args.mode}", data=Path(args.inp).read_bytes())).json())["job"]
        while True:
            job_info = await (await http_session.get(f"{VOICE_SERVICE_URL}/api/job/{job}")).json()
            if job_info["state"] in ("done", "error"):
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)
        await http_session.post(f"{LIVETALKING_URL}/record", json={"type": "end_record", "sessionid": session_id})
        await asyncio.sleep(1.5)
        recording = await http_session.get(f"{LIVETALKING_URL}/record/{session_id}")
        rec_path = ROOT / "outputs/step12_v2" / job_info["job"] / "livetalking_record.mp4"
        if recording.status == 200:
            rec_path.write_bytes(await recording.read())
    await peer_connection.close()

    sample_rate = audio[0][2] if audio else 48000
    received_pcm = np.concatenate([samples for _, samples, _ in audio]) if audio else np.zeros(1)
    first_audio_t = audio[0][0] if audio else 0
    timestamps = job_info["timestamps_s"]
    answer_start = t_ask - first_audio_t + timestamps.get("avatar_speaking_start", 0) - 0.2
    answer_end = t_ask - first_audio_t + timestamps.get("avatar_speaking_end", 0) + 0.2
    gaps_found, span = gaps(received_pcm, sample_rate, answer_start, answer_end)
    import soundfile as sf
    output_dir = ROOT / "outputs/step12_v2" / job_info["job"]
    sf.write(str(output_dir / "webrtc_received_audio.wav"), received_pcm, sample_rate)
    video_times_in_answer = np.array([t for t in video_times if t_ask + timestamps.get("avatar_speaking_start", 0) <= t <= t_ask + timestamps.get("avatar_speaking_end", 1e9)])
    frame_gaps = np.diff(video_times_in_answer) if len(video_times_in_answer) > 2 else np.array([0])
    result = {"job": job_info["job"], "state": job_info["state"], "error": job_info["error"], "mode": args.mode, "sessionid": session_id,
           "transcript": job_info["transcript"], "reply": job_info["reply"], "segments": job_info["segments"], "timestamps_s": timestamps, "metrics": job_info["metrics"],
           "webrtc_viewer": {"audio_gaps_over_60ms_in_answer": gaps_found, "voiced_span_s": span,
                             "video_fps_during_answer": round(len(video_times_in_answer) / max(video_times_in_answer[-1] - video_times_in_answer[0], 1e-6), 2) if len(video_times_in_answer) > 2 else None,
                             "video_max_interframe_ms": round(float(frame_gaps.max()) * 1000, 1), "video_frames_over_60ms": int((frame_gaps > 0.06).sum())},
           "recording": str(rec_path.relative_to(ROOT)) if rec_path.exists() else None}
    (output_dir / "e2e_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


asyncio.run(main())
