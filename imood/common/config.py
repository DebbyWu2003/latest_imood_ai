"""Load pipeline.yaml with defaults; validate the mode."""
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "pipeline.yaml"
MODES = ("vendor", "full")


def load(path=None):
    import yaml
    cfg = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    cfg.setdefault("mode", "vendor")
    if cfg["mode"] not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {cfg['mode']!r}")
    return cfg
