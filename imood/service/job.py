"""One question -> answer job."""
import datetime, time, uuid


def normalize_mode(mode):
    """LLM -> TTS hand-over mode: "sentence" or "full"; anything else falls back to "sentence"."""
    return mode if mode in ("sentence", "full") else "sentence"


class Job:
    def __init__(self, wav, sessionid, mode):
        self.id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
        self.wav, self.sessionid, self.mode = wav, sessionid, mode
        self.state, self.error = "queued", None
        self.transcript, self.reply, self.segments = "", "", []
        self.wall_start = time.time()
        self.perf_start = time.perf_counter()
        self.timestamps, self.metrics, self.log = {"request_received": 0.0}, {}, []

    def mark(self, key, wall=None):
        elapsed = (wall - self.wall_start) if wall else (time.perf_counter() - self.perf_start)
        self.timestamps.setdefault(key, round(elapsed, 3))

    def info(self):
        return {"job": self.id, "state": self.state, "error": self.error, "mode": self.mode, "sessionid": self.sessionid,
                "transcript": self.transcript, "reply": self.reply, "segments": self.segments,
                "timestamps_s": self.timestamps, "metrics": self.metrics, "log": self.log[-30:]}
