"""Source registry (sources.json). ${VAR} placeholders are filled from the environment."""
import json
import os
import re
from pathlib import Path


def load_sources(path, cfg=None):
    text = Path(path).read_text(encoding="utf-8")
    env = dict(os.environ)
    if cfg:
        env.setdefault("DEMO_BASE_URL", cfg.get("DEMO_BASE_URL", "http://127.0.0.1:5000"))
    text = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1)) or m.group(0), text)
    sources = json.loads(text)
    for s in sources:
        if "${" in s.get("url", ""):
            s["enabled"] = False
            s["not_configured"] = True
        for key in ("source_id", "name", "agency", "url", "kind"):
            if not s.get(key):
                raise ValueError(f"sources.json: entry is missing '{key}': {s}")
    return sources
