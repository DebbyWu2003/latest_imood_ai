"""Shared state of the full-mode service process : configuration, loaded models, job table."""
import json, queue
from pathlib import Path

from imood.common.voice_settings import load_voice_config

ROOT = Path(__file__).resolve().parents[2]

VOICE_CONFIG = load_voice_config()
SERVICE_CONFIG = json.loads((ROOT / "config/streaming_service.json").read_text(encoding="utf-8"))
LIVETALKING_CONFIG = SERVICE_CONFIG["livetalking"]
JOBS, JOB_QUEUE = {}, queue.Queue()
MODELS = {}  # loaded models
