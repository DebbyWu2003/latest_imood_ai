"""HTTP API of the full-mode voice service (moved unchanged): POST /api/ask, GET /api/job/<id>, GET /api/health."""
import json, traceback
from http.server import BaseHTTPRequestHandler

from imood.service.job import Job
from imood.service.runner import run_job
from imood.service.state import JOBS, JOB_Q, M, V2


def worker():
    while True:
        job = JOB_Q.get()
        try:
            run_job(job)
        except Exception as e:
            job.error = str(e); job.state = "error"; job.log.append(traceback.format_exc())
        print("JOB", json.dumps(job.info(), ensure_ascii=False), flush=True)



class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/health":
            return self._send(200, {"ok": True, "models": list(M), "setup_s": M.get("_setup")})
        if p.startswith("/api/job/"):
            j = JOBS.get(p.rsplit("/", 1)[1])
            return self._send(200 if j else 404, j.info() if j else {"error": "unknown job"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path != "/api/ask":
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        wav = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if len(wav) < 100:
            return self._send(400, {"error": "empty audio"})
        mode = (q.get("mode") or [V2["llm_to_tts"]["mode"]])[0]
        job = Job(wav, (q.get("sessionid") or [""])[0], mode if mode in ("sentence", "full") else "sentence")
        JOBS[job.id] = job; JOB_Q.put(job)
        self._send(200, {"job": job.id})

    def log_message(self, *a):
        pass
