"""One question -> answer job (moved unchanged)."""
import datetime, time, uuid


class Job:
    def __init__(self, wav, sessionid, mode):
        self.id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
        self.wav, self.sessionid, self.mode = wav, sessionid, mode
        self.state, self.error = "queued", None
        self.transcript, self.reply, self.segments = "", "", []
        self.wall0 = time.time(); self.t0 = time.perf_counter()
        self.ts, self.metrics, self.log = {"request_received": 0.0}, {}, []

    def mark(self, key, wall=None):
        v = (wall - self.wall0) if wall else (time.perf_counter() - self.t0)
        self.ts.setdefault(key, round(v, 3))

    def info(self):
        return {"job": self.id, "state": self.state, "error": self.error, "mode": self.mode, "sessionid": self.sessionid,
                "transcript": self.transcript, "reply": self.reply, "segments": self.segments,
                "timestamps_s": self.ts, "metrics": self.metrics, "log": self.log[-30:]}
