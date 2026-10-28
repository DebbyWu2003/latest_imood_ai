"""Shared state of the full-mode service process : configuration, loaded models, job table."""
import json, queue, threading
from pathlib import Path

from imood.common.voice_settings import load_voice_config

ROOT = Path(__file__).resolve().parents[2]

CFG = load_voice_config()
V2 = json.loads((ROOT / "config/streaming_service.json").read_text(encoding="utf-8"))
LT = V2["livetalking"]
JOBS, JOB_LOCK, JOB_Q = {}, threading.Lock(), queue.Queue()
M = {}  # loaded models
