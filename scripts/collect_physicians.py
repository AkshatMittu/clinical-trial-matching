#!/usr/bin/env python3
"""
Step 2 — build the physician roster.

    python scripts/collect_physicians.py              # resolve seeds + demo pair
    python scripts/collect_physicians.py --limit 60   # try more seed names
    python scripts/collect_physicians.py --seeds-only # show seeds, resolve nothing

Reads data/trials_manifest.json (step 1). Writes data/physicians/{npi}.json,
data/physicians_roster.json, data/physicians_summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trialfit.buckets import build_all, write_buckets
from trialfit.physicians import (harvest_seeds, pick_demo_physicians,
                                 resolve_seeds, write_roster)

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description="Resolve trial investigators to NPIs.")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--limit", type=int, default=40,
                   help="how many seed names to attempt")
    p.add_argument("--seeds-only", action="store_true",
                   help="list harvested seed names and exit")
    p.add_argument("--physicians", type=int, default=3,
                   help="how many demo physicians to build buckets for")
    p.add_argument("--per-tier", type=int, default=3,
                   help="trials per expected-fit tier in each bucket")
    args = p.parse_args()

    data_dir = Path(args.data)
    manifest_path = data_dir / "trials_manifest.json"
    if not manifest_path.exists():
        print("no trials manifest — run scripts/collect_trials.py first")
        return 1
    manifest = json.loads(manifest_path.read_text())

    seeds = harvest_seeds(manifest)
    print(f"seed: {len(seeds)} distinct investigator names from {len(manifest)} trials")
    if args.seeds_only:
        for s in seeds[:args.limit]:
            print(f"  {s.n_trials}x  {s.name:<32} {', '.join(s.trials[:3])}")
        return 0

    print(f"\nresolving against NPPES (top {args.limit}) —")
    roster, funnel = resolve_seeds(seeds, limit=args.limit)

    gold_ids = set()
    gold_path = data_dir / "trials_gold_ids.txt"
    if gold_path.exists():
        gold_ids = {l.strip() for l in gold_path.read_text().splitlines() if l.strip()}
    roster = pick_demo_physicians(roster, gold_ids, n=args.physicians)

    attempted = min(args.limit, len(seeds))
    print(f"\n  funnel: {attempted} attempted -> "
          + ", ".join(f"{v} {k}" for k, v in sorted(funnel.items())))

    summary = write_roster(roster, funnel, data_dir)

    print("\n  demo physicians —")
    for d in summary["demo"]:
        print(f"    {d['name']:<24} {d['npi']}  {d['specialty']}")
        print(f"    {'':<24} {d['city']}, {d['state']} · "
              f"{d['n_source_trials']} trials, {len(d['gold_trials'])} gold")

    print("\nbuilding trial buckets —")
    buckets = build_all(roster, manifest, per_tier=args.per_tier)
    bsum = write_buckets(buckets, data_dir)
    for b in buckets:
        counts = " ".join(f"{t}={n}" for t, n in b["tier_counts"].items() if n)
        print(f"    {b['physician']:<24} {b['n_trials']:>2} trials   {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
