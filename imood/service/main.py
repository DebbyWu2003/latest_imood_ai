"""Full-mode voice service (P2) entry: load models once, start the worker and the HTTP server (:8020)."""
import json, os, sys, threading
from http.server import ThreadingHTTPServer

from imood.intake import mic_ws, transcriber
from imood.service.http_api import ApiHandler, run_job_worker
from imood.service.job import Job, normalize_mode
from imood.service.state import JOBS, JOB_QUEUE, MODELS, ROOT, SERVICE_CONFIG, VOICE_CONFIG
from imood.voice import tts_voice


def submit_text(text, wav, sessionid):
    """Microphone path: queue a job whose transcript is already known (same worker, same run_job)."""
    mode = SERVICE_CONFIG["llm_to_tts"]["mode"]
    job = Job(wav, sessionid, normalize_mode(mode))
    job.transcript = text
    JOBS[job.id] = job
    JOB_QUEUE.put(job)
    return job.id


def load_models():
    asr_config, tts_config, tts_fast_config = VOICE_CONFIG["asr"], VOICE_CONFIG["tts"], SERVICE_CONFIG["tts_fast"]
    timings = {}
    MODELS["asr"], timings["asr_load_s"] = transcriber.load(asr_config)
    MODELS["tts"], tts_timings = tts_voice.load(tts_config, tts_fast_config, str(ROOT / tts_config["ref_audio"]), VOICE_CONFIG["ref_text"])
    timings.update(tts_timings)
    MODELS["_setup"] = timings
    print("SETUP", json.dumps(timings), flush=True)


def main():
    if os.environ.get("WSL_DISTRO_NAME") != SERVICE_CONFIG["wsl_distro"]:
        sys.exit(f"Must run in WSL {SERVICE_CONFIG['wsl_distro']} (current: {os.environ.get('WSL_DISTRO_NAME')!r})")
    load_models()
    threading.Thread(target=run_job_worker, daemon=True).start()
    mic_ws.start(submit_text, MODELS["asr"], VOICE_CONFIG["asr"])
    server = ThreadingHTTPServer((SERVICE_CONFIG["service"]["host"], SERVICE_CONFIG["service"]["port"]), ApiHandler)
    print(f"READY voice service on :{SERVICE_CONFIG['service']['port']}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
