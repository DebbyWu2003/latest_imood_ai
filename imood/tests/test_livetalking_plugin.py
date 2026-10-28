"""Behaviour-lock test for imood/downstream/livetalking_plugin.py with fake LiveTalking sessions (no GPU, no browser).

The plugin imports LiveTalking's own modules, so run it in the LiveTalking venv from the repo root:
  /root/livetalking/LiveTalking/.venv-lt/bin/python -m imood.tests.test_livetalking_plugin
"""
import asyncio, json, logging, queue, threading, time
from types import SimpleNamespace
from pathlib import Path

import numpy as np

import imood.downstream.livetalking_plugin as plugin

FRAME = 320


class FakeAsr:
    def __init__(self, frames=None):
        self.queue = queue.Queue()
        self.frames = list(frames or [])

    def get_audio_frame(self):
        return self.frames.pop(0)


class FakeSession:
    def __init__(self, avatar_id="wav2lip256_avatar3_WA3", frames=None):
        self.asr = FakeAsr(frames)
        self.opt = SimpleNamespace(avatar_id=avatar_id)
        self.msgqueues = []
        self.speaking = False

    def add_msgqueue(self, q):
        self.msgqueues.append(q)

    def is_speaking(self):
        return self.speaking


class FakeRequest:
    def __init__(self, query, chunks=()):
        self.query = query
        self.content = self
        self._chunks = list(chunks)

    async def iter_any(self):
        for c in self._chunks:
            yield c


def call(handler, request):
    resp = asyncio.run(handler(request))
    return json.loads(resp.text)


def drain(q):
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def samples(n):
    return (np.sin(np.arange(n) / 7.0) * 0.5).astype("<f4")


def with_session(sid, session):
    plugin.session_manager.sessions[sid] = session


def drop_session(sid):
    plugin.session_manager.sessions.pop(sid, None)
    plugin.ACTIVE_UTTERANCES.pop(sid, None)


def test_audio_stream_unknown_session():
    assert call(plugin.audio_stream, FakeRequest({"sessionid": "nope"})) == {"code": -1, "msg": "session not found"}


def test_audio_stream_short_answer_is_held_then_flushed_with_end_marker():
    sid, sess = "t-short", FakeSession()
    with_session(sid, sess)
    try:
        pcm = samples(1000)
        raw = pcm.tobytes()
        r = call(plugin.audio_stream, FakeRequest({"sessionid": sid, "utt": "u-short"}, [raw[:1500], raw[1500:]]))
        assert r["code"] == 0 and r["msg"] == "ok"
        d = r["data"]
        assert (d["utt"], d["sessionid"], d["frames"], d["bytes"], d["http_chunks"]) == ("u-short", sid, 4, 4000, 2)
        assert (d["max_queue_frames"], d["overflows"], d["underruns"], d["underrun_frames"], d["audio_s"]) == (5, 0, 0, 0, 0.08)
        assert d["underrun_at_s"] == [] and "_in_underrun" not in d and d["first_frame_put"] <= d["last_frame_put"] <= d["eof_at"]
        items = drain(sess.asr.queue)
        assert len(items) == 5
        assert items[0].userdata == {"status": "start", "cyber_gf_utt": "u-short"}
        assert all(it.userdata == {} for it in items[1:4]) and all(it.type == 0 for it in items)
        assert items[4].userdata == {"status": "end", "cyber_gf_utt": "u-short"} and not items[4].data.any()
        assert np.array_equal(items[0].data, pcm[:FRAME]) and np.array_equal(items[2].data, pcm[2 * FRAME:3 * FRAME])
        last = np.zeros(FRAME, np.float32); last[:40] = pcm[960:]               # partial last frame is zero-padded
        assert np.array_equal(items[3].data, last)
        assert plugin.UTTERANCE_STATS["u-short"]["utt"] == "u-short" and plugin.ACTIVE_UTTERANCES[sid] is plugin.UTTERANCE_STATS["u-short"]
        assert getattr(sess.asr, "_cyber_gf_wrapped", False) is True and sid in plugin.PLAYBACK_LISTENERS
    finally:
        drop_session(sid)


def test_audio_stream_preroll_releases_blocks_early():
    sid, sess = "t-preroll", FakeSession()
    with_session(sid, sess)
    try:
        pcm = samples(FRAME * 6)
        raw = pcm.tobytes()
        chunks = [raw[i:i + 1280 * 2] for i in range(0, len(raw), 1280 * 2)]       # 2 frames per chunk
        r = call(plugin.audio_stream, FakeRequest({"sessionid": sid, "utt": "u-pre", "preroll_frames": "2"}, chunks))
        d = r["data"]
        assert (d["frames"], d["http_chunks"], d["bytes"], d["audio_s"]) == (6, 3, len(raw), 0.12)
        items = drain(sess.asr.queue)
        assert [it.userdata.get("status") for it in items] == ["start", None, None, None, None, None, "end"]
        assert np.array_equal(np.concatenate([it.data for it in items[:6]]), pcm)
        assert d["max_queue_frames"] == 7
    finally:
        drop_session(sid)


def test_audio_stream_empty():
    sid, sess = "t-empty", FakeSession()
    with_session(sid, sess)
    try:
        r = call(plugin.audio_stream, FakeRequest({"sessionid": sid, "utt": "u-empty"}, []))
        assert r["code"] == -1 and r["msg"] == "empty audio stream" and r["data"]["frames"] == 0
        assert sess.asr.queue.empty()
    finally:
        drop_session(sid)


def test_utt_status_and_stats():
    sid, sess = "t-stats", FakeSession()
    with_session(sid, sess)
    try:
        call(plugin.audio_stream, FakeRequest({"sessionid": sid, "utt": "u-stats"}, [samples(640).tobytes()]))
        plugin.LIVETALKING_FPS.clear(); plugin.LIVETALKING_FPS["final"] = 25.0
        r = call(plugin.utt_status, SimpleNamespace(match_info={"utt": "u-stats"}))
        assert r["code"] == 0 and r["data"]["queue_frames_now"] == 3 and r["data"]["lt_fps"] == {"final": 25.0}
        assert r["data"]["utt"] == "u-stats" and "_in_underrun" not in r["data"]
        assert call(plugin.utt_status, SimpleNamespace(match_info={"utt": "zzz"})) == {"code": -1, "msg": "unknown utt"}
        s = call(plugin.stats_route, SimpleNamespace())
        assert s["code"] == 0 and s["data"]["lt_fps"] == {"final": 25.0}
        mine = [x for x in s["data"]["sessions"] if x["sessionid"] == sid]
        assert mine == [{"sessionid": sid, "avatar_id": "wav2lip256_avatar3_WA3", "queue_frames": 3, "speaking": False}]
        assert set(s["data"]["cuda_mem_gb"]) in ({"allocated", "reserved", "max_allocated", "max_reserved"}, {"error"})
        assert s["data"]["recent_utts"][-1]["utt"] == "u-stats"
    finally:
        drop_session(sid)
        plugin.LIVETALKING_FPS.clear()


def test_underrun_counter():
    sid = "t-underrun"
    sound = lambda ud=None: plugin.AudioFrameData(data=np.ones(FRAME, np.float32), type=0, userdata=ud or {})
    silence = lambda: plugin.AudioFrameData(data=np.zeros(FRAME, np.float32), type=1, userdata={})
    end = lambda: plugin.AudioFrameData(data=np.zeros(FRAME, np.float32), type=0, userdata={"status": "end", "cyber_gf_utt": "u-un"})
    seq = [silence(), sound({"status": "start", "cyber_gf_utt": "u-un"}), silence(), silence(), sound(), silence(), end(), silence()]
    sess = FakeSession(frames=seq)
    st = {"utt": "u-un", "underrun_frames": 0, "underruns": 0, "underrun_at_s": [], "_in_underrun": False}
    plugin.ACTIVE_UTTERANCES[sid] = st
    try:
        plugin._install_underrun_counter(sid, sess)
        wrapped = sess.asr.get_audio_frame
        plugin._install_underrun_counter(sid, sess)
        assert sess.asr.get_audio_frame is wrapped                       # wrapped once only
        for _ in range(len(seq)):
            sess.asr.get_audio_frame()
        assert (st["underrun_frames"], st["underruns"], len(st["underrun_at_s"])) == (3, 2, 2)
        assert "consumed_start" in st and "consumed_end" in st and st["_in_underrun"] is False
    finally:
        plugin.ACTIVE_UTTERANCES.pop(sid, None)


def test_playback_listener_records_start_and_end():
    sid, sess = "t-listen", FakeSession()
    with_session(sid, sess)
    st = {"utt": "u-listen", "sessionid": sid}
    plugin._record_utterance(st)
    try:
        plugin._listen_for_playback(sid, sess)
        plugin._listen_for_playback(sid, sess)                                          # second call is a no-op
        assert len(sess.msgqueues) == 1
        sess.msgqueues[0].put(json.dumps({"status": "start", "cyber_gf_utt": "u-listen"}))
        sess.msgqueues[0].put(json.dumps({"status": "end", "cyber_gf_utt": "u-listen"}))
        sess.msgqueues[0].put("not json")
        sess.msgqueues[0].put(json.dumps({"status": "start", "cyber_gf_utt": "other"}))
        end = time.time() + 5
        while time.time() < end and "playback_end" not in st:
            time.sleep(0.02)
        assert "playback_start" in st and "playback_end" in st and st["playback_start"] <= st["playback_end"]
    finally:
        drop_session(sid)
        end = time.time() + 5
        while time.time() < end and sid in plugin.PLAYBACK_LISTENERS:
            time.sleep(0.05)
        assert sid not in plugin.PLAYBACK_LISTENERS


def test_remember_keeps_last_100():
    plugin.UTTERANCE_STATS.clear()
    for i in range(105):
        plugin._record_utterance({"utt": f"r{i}"})
    assert len(plugin.UTTERANCE_STATS) == 100 and next(iter(plugin.UTTERANCE_STATS)) == "r5"
    plugin.UTTERANCE_STATS.clear()


def test_fps_capture():
    plugin.LIVETALKING_FPS.clear()
    rec = logging.LogRecord("x", logging.INFO, "", 0, "actual avg infer fps:101.5 and actual avg final fps:25.0", None, None)
    plugin._FpsCapture().emit(rec)
    assert plugin.LIVETALKING_FPS["infer"] == 101.5 and "infer_at" in plugin.LIVETALKING_FPS and "final" not in plugin.LIVETALKING_FPS
    plugin.LIVETALKING_FPS.clear()


def test_session_lookup_and_emotion_route():
    a, b = FakeSession(), FakeSession(avatar_id="wav2lip256_emo_calm")
    with_session("t-a", a); with_session("t-b", b)
    try:
        assert plugin._latest_session() is b
        assert plugin._find_session("t-a") == ("t-a", a)
        assert plugin._find_session("missing") == ("t-b", b)
        assert plugin._find_session("") == ("t-b", b)
        r = call(plugin.emotion_route, SimpleNamespace(query={"sessionid": "none", "emotion": "happy"}))
        assert r == {"code": -1, "msg": "session not found"}
        r = call(plugin.emotion_route, SimpleNamespace(query={"sessionid": "t-a", "emotion": "happy"}))
        assert r == {"code": -1, "msg": "session avatar is not an emotion avatar (wav2lip256_emo_*)"}
        r = call(plugin.emotion_route, SimpleNamespace(query={"emotion": "happy"}))            # no face lists on the fake session
        assert r == {"code": -1, "msg": "session avatar is not an emotion avatar (wav2lip256_emo_*)"}
    finally:
        drop_session("t-a"); drop_session("t-b")
    assert plugin._latest_session() is None and plugin._find_session("zzz") == (None, None)


def test_routes_and_constants():
    assert (plugin.FRAME, plugin.FRAME_BYTES, plugin.OVERFLOW_FRAMES, plugin.PREROLL_FRAMES) == (320, 1280, 3000, 24)
    assert plugin._public({"a": 1, "_b": 2}) == {"a": 1}
    assert plugin.imood_cfg.load()["downstream"]["port"] == 8100


TESTS = [test_audio_stream_unknown_session, test_audio_stream_short_answer_is_held_then_flushed_with_end_marker,
         test_audio_stream_preroll_releases_blocks_early, test_audio_stream_empty, test_utt_status_and_stats,
         test_underrun_counter, test_playback_listener_records_start_and_end, test_remember_keeps_last_100,
         test_fps_capture, test_session_lookup_and_emotion_route, test_routes_and_constants]


if __name__ == "__main__":
    for t in TESTS:
        t()
        print("ok", t.__name__)
    print("livetalking_plugin: ALL PASS")
