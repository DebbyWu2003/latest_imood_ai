"""Shared state of the full-mode service process : configuration, loaded models, job table."""
import json, queue, sys, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "wsl"))            # helpers: load_cfg, discover_session, lt_get
from voice_pipeline_test import load_cfg                      # voice.json + persona + ref_text

CFG = load_cfg()
V2 = json.loads((ROOT / "config/step12_v2.json").read_text(encoding="utf-8"))
LT = V2["livetalking"]
JOBS, JOB_LOCK, JOB_Q = {}, threading.Lock(), queue.Queue()
M = {}  # loaded models
