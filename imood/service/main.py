"""Full-mode voice service (P2) entry: load models once, start the worker and the HTTP server (:8020)."""
import json, os, sys, threading, time
from http.server import ThreadingHTTPServer

from imood.intake import transcriber
from imood.service.http_api import H, worker
from imood.service.state import CFG, M, ROOT, V2
from imood.voice import tts_voice


def load_models():
    a, c, tf = CFG["asr"], CFG["tts"], V2["tts_fast"]
    s = {}
    M["asr"], s["asr_load_s"] = transcriber.load(a)
    M["tts"], tts_s = tts_voice.load(c, tf, str(ROOT / c["ref_audio"]), CFG["ref_text"])
    s.update(tts_s)
    M["_setup"] = s
    print("SETUP", json.dumps(s), flush=True)


def main():
    if os.environ.get("WSL_DISTRO_NAME") != V2["wsl_distro"]:
        sys.exit(f"Must run in WSL {V2['wsl_distro']} (current: {os.environ.get('WSL_DISTRO_NAME')!r})")
    load_models()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((V2["service"]["host"], V2["service"]["port"]), H)
    print(f"READY voice service on :{V2['service']['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
