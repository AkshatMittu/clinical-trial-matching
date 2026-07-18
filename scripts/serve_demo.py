#!/usr/bin/env python3
"""
The demo UI.

    python scripts/serve_demo.py            # http://127.0.0.1:8765
    python scripts/serve_demo.py --port 9000

Standard library only — nothing to install. Replay mode needs no API key and no
network; live mode runs the pipeline for real.
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trialfit import server

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description="Serve the trial_fit demo UI.")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    if not args.no_open:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    server.serve(Path(args.data), port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
