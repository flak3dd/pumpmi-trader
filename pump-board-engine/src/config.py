import os, yaml
from pathlib import Path
DEFAULTS = {"mode": "paper"}
def load(path=None):
    p = Path(path or os.environ.get("PUMP_CONFIG", "config.yaml"))
    if not p.exists(): p = Path("config.example.yaml")
    cfg = {**DEFAULTS, **yaml.safe_load(p.read_text())}
    if cfg["mode"] not in ("paper", "live"): raise ValueError("mode must be paper|live")
    return cfg
