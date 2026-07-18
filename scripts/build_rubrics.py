#!/usr/bin/env python3
"""
Step 4 — the evaluator agent: build a tailored rubric per trial.

    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/build_rubrics.py --dry-run     # show a brief, no API call
    python scripts/build_rubrics.py --only NCT02513394
    python scripts/build_rubrics.py               # every trial in the buckets

Reads data/trial_info/{NCT}.json (step 3). Writes data/rubrics/{NCT}.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trialfit import rubric as R

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description="Build per-trial scoring rubrics.")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--only", help="build one trial by NCT id")
    p.add_argument("--dry-run", action="store_true",
                   help="print the assembled brief and exit, no API call")
    p.add_argument("--force", action="store_true", help="rebuild existing rubrics")
    p.add_argument("--limit", type=int, default=0, help="cap how many to build")
    p.add_argument("--model", default=R.MODEL,
                   help=f"model to use (default {R.MODEL})")
    p.add_argument("--budget", type=float, default=0.0,
                   help="stop once this much USD has been spent this run")
    args = p.parse_args()

    data_dir = Path(args.data)
    info_dir = data_dir / "trial_info"
    if not info_dir.exists():
        print("no trial_info — run scripts/collect_evidence.py --trials first")
        return 1

    if args.only:
        ncts = [args.only]
    else:
        pairs_path = data_dir / "scoring_pairs.json"
        if not pairs_path.exists():
            print("no scoring_pairs.json — run scripts/collect_physicians.py first")
            return 1
        ncts = sorted({p["nct_id"] for p in json.loads(pairs_path.read_text())})
    if args.limit:
        ncts = ncts[:args.limit]

    if args.dry_run:
        nct = ncts[0]
        info = json.loads((info_dir / f"{nct}.json").read_text())
        brief = R.build_brief(info)
        print(brief)
        approx_in = len(brief) // 4
        rates = R.PRICING.get(args.model)
        est = ""
        if rates:
            # Output dominates: a rubric plus adaptive thinking runs ~4k tokens.
            est = (f" | est. ~${(approx_in / 1e6 * rates[0]) + (4000 / 1e6 * rates[1]):.3f}"
                   f"/trial, ~${((approx_in / 1e6 * rates[0]) + (4000 / 1e6 * rates[1])) * 19:.2f} for all 19")
        print(f"\n--- brief: {len(brief)} chars, ~{approx_in} tokens "
              f"(model={args.model}, effort={R.EFFORT}){est} ---")
        return 0

    print(f"evaluator agent — {len(ncts)} trial(s), model={args.model}, "
          f"effort={R.EFFORT}"
          + (f", budget ${args.budget:.2f}" if args.budget else "") + "\n")
    built = failed = 0
    spent = 0.0
    for nct in ncts:
        out = data_dir / "rubrics" / f"{nct}.json"
        if out.exists() and not args.force:
            print(f"  {nct}  cached")
            continue
        info_path = info_dir / f"{nct}.json"
        if not info_path.exists():
            print(f"  {nct}  ! no trial_info")
            failed += 1
            continue

        if args.budget and spent >= args.budget:
            print(f"\n  budget ${args.budget:.2f} reached (spent ${spent:.2f}) "
                  f"— stopping with {len(ncts) - built - failed} trial(s) left")
            break

        info = json.loads(info_path.read_text())
        label = info.get("acronym") or info.get("brief_title", "")[:34]
        print(f"  {nct}  {label}")
        try:
            record = R.build(info, model=args.model)
        except Exception as e:
            print(f"    ! {type(e).__name__}: {e}")
            failed += 1
            continue

        R.write(record, data_dir)
        s, prov = record["scoring"], record["provenance"]
        cost = record["trace"]["cost_usd"]
        spent += cost
        gates = ", ".join(s["hard_gates"]) or "none"
        print(f"    {s['n_criteria']} criteria, max {s['max_score']} pts | "
              f"gate: {gates}")
        print(f"    burden={prov['burden_source']} | public coverage "
              f"{s['public_coverage']:.0%} "
              f"({s['n_self_report_only']} self-report only)")
        print(f"    ${cost:.4f}  (running total ${spent:.2f})")
        built += 1

    print(f"\n  built {built}, failed {failed} -> {data_dir}/rubrics/")
    print(f"  spent ${spent:.2f} on {args.model}")
    return 1 if failed and not built else 0


if __name__ == "__main__":
    raise SystemExit(main())
