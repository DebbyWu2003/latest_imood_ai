"""Voice settings: config/voice.json plus the persona and reference-text files it points to."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_voice_config():
    cfg = json.loads((ROOT / "config/voice.json").read_text(encoding="utf-8"))
    cfg["persona"] = (ROOT / cfg["llm"]["persona_file"]).read_text(encoding="utf-8").strip()
    cfg["ref_text"] = (ROOT / cfg["tts"]["ref_text_file"]).read_text(encoding="utf-8").strip()
    return cfg
