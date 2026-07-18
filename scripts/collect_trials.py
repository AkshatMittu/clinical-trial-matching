#!/usr/bin/env python3
"""
Step 1 — build the sample trial set.

    python scripts/collect_trials.py --recon        # funnel sizes, pulls nothing
    python scripts/collect_trials.py                # collect with defaults
    python scripts/collect_trials.py --max 150      # smaller pull
    python scripts/collect_trials.py --no-focused   # skip the subtype narrowing

Writes to data/:  trials_raw/{NCT}.json, trials_manifest.{csv,json},
trials_{gold,demo,broad}_ids.txt, collection_summary.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trialfit.collect import (SCOPES, Limits, collect, rebuild_from_cache, recon,
                              write_outputs)

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description="Collect sample clinical trials.")
    p.add_argument("--scope", default="breast_hr_pos", choices=sorted(SCOPES),
                   help="which slice of ClinicalTrials.gov to pull")
    p.add_argument("--recon", action="store_true",
                   help="report funnel sizes and exit without pulling")
    p.add_argument("--max", type=int, default=400, dest="max_records",
                   help="cap on candidate records pulled")
    p.add_argument("--gold", type=int, default=40, help="cap on the gold set")
    p.add_argument("--demo", type=int, default=15, help="cap on the demo set")
    p.add_argument("--broad", type=int, default=250, help="cap on the broad set")
    p.add_argument("--no-focused", action="store_true",
                   help="query the condition only, without the subtype term")
    p.add_argument("--from-cache", action="store_true",
                   help="re-derive the manifest from cached records, no API calls")
    p.add_argument("--data", default=str(ROOT / "data"), help="output directory")
    args = p.parse_args()

    scope = SCOPES[args.scope]
    data_dir = Path(args.data)

    print(f"scope: {scope.name}  (condition={scope.condition!r})")

    if args.from_cache:
        limits = Limits(max_records=args.max_records, n_gold=args.gold,
                        n_demo=args.demo, n_broad=args.broad)
        metas = rebuild_from_cache(scope, data_dir)
        if not metas:
            print("  no cached records — run without --from-cache first")
            return 1
        write_outputs(metas, scope, limits, data_dir)
        return 0

    print("\nrecon —")
    funnel = recon(scope)
    print(f"  {scope.condition}, any status         {funnel['broad_total']:>6,}")
    print(f"  + subtype-focused term               {funnel['focused_total']:>6,}")
    if args.recon:
        return 0

    print(f"\ncollecting (max {args.max_records}) —")
    limits = Limits(max_records=args.max_records, n_gold=args.gold,
                    n_demo=args.demo, n_broad=args.broad)
    metas = collect(scope, limits, data_dir, use_focused=not args.no_focused)
    if not metas:
        print("  no trials matched — try --no-focused or a wider --max")
        return 1

    summary = write_outputs(metas, scope, limits, data_dir)

    gold = [m for m in metas if m["bucket"] == "gold"][:5]
    if gold:
        print("\n  top gold trials —")
        for m in gold:
            label = m["acronym"] or m["brief_title"][:44]
            print(f"    {m['nct_id']}  q={m['quality_score']:>4}  "
                  f"{'PDF' if m['has_protocol'] else '   '}  {label}")

    missing = set(scope.anchors) - set(summary["anchors_present"])
    if missing:
        print(f"\n  ! anchors missing: {', '.join(sorted(missing))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
