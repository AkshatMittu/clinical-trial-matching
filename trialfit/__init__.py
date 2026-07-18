"""trialfit — physician/clinical-trial matching pipeline."""
from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> list[str]:
    """Load KEY=value pairs from .env into the environment.

    Stdlib only — no python-dotenv dependency for twenty lines of parsing. A
    real environment variable always wins over the file, so `ANTHROPIC_API_KEY=…
    python script.py` overrides .env for one run without editing anything.

    Returns the names (never the values) of the keys it set.
    """
    path = path or _ROOT / ".env"
    if not path.exists():
        return []
    loaded = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


# Imported by every entry point, so a .env is picked up wherever you start.
load_env()
