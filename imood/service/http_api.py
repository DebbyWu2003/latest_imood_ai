"""HTTP API of the full-mode voice service : POST /api/ask, GET /api/job/<id>, GET /api/health."""
import json, traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from imood.service.job import Job, normalize_mode
from imood.service.runner import run_job
from imood.service.state import JOBS, JOB_QUEUE, MODELS, SERVICE_CONFIG


def run_job_worker():
    while True:
        job = JOB_QUEUE.get()
        try:
            run_job(job)
        except Exception as error:
            job.error = str(error)
            job.state = "error"
            job.log.append(traceback.format_exc())
        print("JOB", json.dumps(job.info(), ensure_ascii=False), flush=True)



class ApiHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self._send_json(200, {"ok": True, "models": list(MODELS), "setup_s": MODELS.get("_setup")})
        if path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[1])
            return self._send_json(200 if job else 404, job.info() if job else {"error": "unknown job"})
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/ask":
            return self._send_json(404, {"error": "not found"})
        query = parse_qs(url.query)
        wav = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if len(wav) < 100:
            return self._send_json(400, {"error": "empty audio"})
        mode = (query.get("mode") or [SERVICE_CONFIG["llm_to_tts"]["mode"]])[0]
        job = Job(wav, (query.get("sessionid") or [""])[0], normalize_mode(mode))
        JOBS[job.id] = job
        JOB_QUEUE.put(job)
        self._send_json(200, {"job": job.id})

    def log_message(self, *args):
        pass
