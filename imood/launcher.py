"""Start / stop / status of the services for one MODE (WSL side).

  python -m imood.launcher start [vendor|full]     MODE argument > env MODE > imood/pipeline.yaml `mode`
  python -m imood.launcher stop
  python -m imood.launcher status

build_plan(mode) is the one place that decides which services a mode needs (Factory); each mode is a different
list built from the same Service description (Strategy):

  vendor : LiveTalking + adapter only. Starts through scripts/wsl/start_avatar_services.sh with LEGACY_VOICE=0.
  full   : LiveTalking + BERT emotion service (UDP) + voice service (ASR, LLM client, TTS, microphone WS).
           llama-server (:8090) runs on Windows; this launcher only checks it.

Paths and venvs come from the environment (config/paths.env, sourced by scripts/wsl/start.sh).
"""
import os, subprocess, sys, time, urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from imood.common import config as cfgmod

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Service:
    name: str
    argv: list
    log: str
    ready: object                     # callable -> bool
    env: dict = field(default_factory=dict)
    stop_patterns: tuple = ()         # pkill -f patterns
    foreground: bool = False          # the command itself waits until ready and then exits (existing start script)
    ready_timeout: int = 180


def _http_ok(url, needle):
    def check():
        try:
            return needle in urllib.request.urlopen(url, timeout=2).read().decode("utf-8", "replace")
        except Exception:
            return False
    return check


def _udp_emotion_ok(host, port):
    def check():
        from imood.emotion.udp_client import EmotionClientSync
        client = EmotionClientSync(host, port, 1500)
        client.start("測試")
        time.sleep(1.2)
        return client.result()[1]
    return check


def build_plan(mode, cfg=None, env=None):
    """-> list of Service in start order."""
    cfg = cfg or cfgmod.load()
    env = env if env is not None else os.environ
    if mode not in cfgmod.MODES:
        raise ValueError(f"mode must be one of {cfgmod.MODES}, got {mode!r}")
    root = str(ROOT)
    log_dir = f"{root}/logs"
    livetalking_url = cfg.get("livetalking", {}).get("url", "http://127.0.0.1:8010")
    plan = [Service(
        name="livetalking", argv=["bash", f"{root}/scripts/wsl/start_avatar_services.sh"], log=f"{log_dir}/start_livetalking.log",
        ready=_http_ok(f"{livetalking_url}/cyber_gf/stats", '"code": 0'), env={"LEGACY_VOICE": "0"},
        stop_patterns=("livetalking_cyber_gf.py",), foreground=True)]
    if mode == "vendor":
        return plan

    venv = env.get("TTS_FAST_VENV", "/root/cyber_gf/.venv-tts-fast")
    venv_python = f"{venv}/bin/python"
    emotion_config = cfg.get("emotion", {})
    if emotion_config.get("enabled", True):
        plan.append(Service(
            name="emotion", argv=[venv_python, "-m", "imood.emotion.udp_service"], log=f"{log_dir}/emotion_udp.log",
            ready=_udp_emotion_ok(emotion_config.get("udp_host", "127.0.0.1"), int(emotion_config.get("udp_port", 8102))),
            env={"EMOTION_UDP_HOST": emotion_config.get("udp_host", "127.0.0.1"), "EMOTION_UDP_PORT": str(emotion_config.get("udp_port", 8102)),
                 "HF_HOME": env.get("HF_HOME", "/root/.cache/huggingface")},
            stop_patterns=("imood.emotion.udp_service",), ready_timeout=120))
    site_packages = subprocess.run([venv_python, "-c", "import site;print(site.getsitepackages()[0])"], capture_output=True, text=True).stdout.strip() \
        if Path(venv_python).exists() else ""
    library_path = f"{site_packages}/nvidia/cublas/lib:{site_packages}/nvidia/cudnn/lib:" + env.get("LD_LIBRARY_PATH", "")
    plan.append(Service(
        name="voice", argv=[venv_python, f"{root}/scripts/wsl/voice_service.py"], log=f"{log_dir}/voice_service.log",
        ready=_http_ok("http://127.0.0.1:8020/api/health", '"ok": true'),
        env={"HF_HOME": env.get("HF_HOME", "/root/.cache/huggingface"), "LD_LIBRARY_PATH": library_path},
        stop_patterns=("wsl/voice_service.py",), ready_timeout=240))
    return plan


def _kill(patterns):
    own_pids = {os.getpid(), os.getppid()}
    killed = 0
    for pattern in patterns:
        pids_output = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
        for pid in (int(pid_text) for pid_text in pids_output if pid_text.isdigit()):
            if pid not in own_pids:
                try:
                    os.kill(pid, 15); killed += 1
                except ProcessLookupError:
                    pass
    return killed


def stop(plan=None):
    plan = plan or build_plan("full")
    for service in reversed(plan):
        print(f"{service.name}: {'stopped' if _kill(service.stop_patterns) else 'not running'}", flush=True)


def start(mode):
    plan = build_plan(mode)
    llama_ok = _http_ok("http://127.0.0.1:8090/health", "ok")()
    if mode == "full" and not llama_ok:
        print("WARNING: llama-server not reachable on :8090 (scripts\\windows\\start.bat full starts it)", flush=True)
    stop(build_plan("full"))                        # clean start: stop what a previous mode left running
    time.sleep(2)
    Path(ROOT / "logs").mkdir(exist_ok=True)
    failed = False
    for service in plan:
        env = {**os.environ, **service.env, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        with open(service.log, "w") as log:
            process = subprocess.Popen(service.argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                 cwd=str(ROOT), start_new_session=True)
        if service.foreground:
            process.wait()
        deadline = time.time() + service.ready_timeout
        while not service.ready() and time.time() < deadline:
            time.sleep(2)
        ok = service.ready()
        failed |= not ok
        print(f"{service.name}: {'OK' if ok else 'NOT READY'}  (log {service.log})", flush=True)
    print(f"mode={mode}  open: http://localhost:8010/imood/imood_ai.html", flush=True)
    return 1 if failed else 0


def status():
    for service in build_plan("full"):
        print(f"{service.name}: {'up' if service.ready() else 'down'}", flush=True)


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    command = args[0] if args else "status"
    if command == "start":
        mode = (args[1] if len(args) > 1 else os.environ.get("MODE")) or cfgmod.load().get("mode", "vendor")
        sys.exit(start(mode))
    elif command == "stop":
        stop()
    elif command == "status":
        status()
    else:
        sys.exit("usage: python -m imood.launcher start [vendor|full] | stop | status")


if __name__ == "__main__":
    main()
