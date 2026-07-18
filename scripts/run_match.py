#!/usr/bin/env python3
"""
Step 5 — run the match end to end, or replay a recorded run for a demo.

Live (needs ANTHROPIC_API_KEY, ~30-90s per pair):
    python scripts/run_match.py --nct NCT02513394 --npi 1336121789
    python scripts/run_match.py --prebuild            # every demo pair, then stop

Demo (no key, no network, instant):
    python scripts/run_match.py --replay --nct NCT02513394 --npi 1336121789
    python scripts/run_match.py --list                # what's already recorded

Answers for the gap-closing interview come from data/demo_answers.json, keyed
"{NCT}__{NPI}" -> {criterion_id: answer}. Without them the interview still runs
and shows the questions, but nothing is re-scored.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trialfit import pipeline
from trialfit.rubric import MODEL

ROOT = Path(__file__).resolve().parent.parent

TICK = {"pending": "·", "running": "⋯", "done": "✓", "skipped": "–", "error": "✗"}


def printer(step) -> None:
    if step.status == "running":
        print(f"  {TICK['running']} {step.label} ...", flush=True)
        return
    cost = f"  ${step.cost_usd:.4f}" if step.cost_usd else ""
    secs = f"  {step.seconds:.1f}s" if step.seconds else ""
    print(f"  {TICK.get(step.status, '?')} {step.label}{secs}{cost}")
    if step.detail:
        print(f"      {step.detail}")


def load_answers(data_dir: Path, nct: str, npi: str) -> dict:
    path = data_dir / "demo_answers.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get(f"{nct}__{npi}", {})


def demo_pairs(data_dir: Path) -> list[tuple[str, str]]:
    """Pairs worth prebuilding.

    With a demo_config.json, exactly the pairs it names — one physician across a
    fixed spread of tiers. Without one, every `known` pair plus one `weak` per
    physician, which is the widest useful default.
    """
    cfg_path = data_dir / "demo_config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        if cfg.get("npi") and cfg.get("nct_ids"):
            return [(n, cfg["npi"]) for n in cfg["nct_ids"]]
    pairs = json.loads((data_dir / "scoring_pairs.json").read_text())
    out, seen_weak = [], set()
    for p in pairs:
        if p["tier"] == "known":
            out.append((p["nct_id"], p["npi"]))
        elif p["tier"] == "weak" and p["npi"] not in seen_weak:
            seen_weak.add(p["npi"])
            out.append((p["nct_id"], p["npi"]))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Run or replay one match.")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--nct")
    p.add_argument("--npi")
    p.add_argument("--replay", action="store_true", help="replay from disk, no API")
    p.add_argument("--pace", type=float, default=0.15,
                   help="replay pacing (0=instant, 1=original speed)")
    p.add_argument("--prebuild", action="store_true",
                   help="run every demo pair live and record trajectories")
    p.add_argument("--list", action="store_true", help="list recorded trajectories")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--no-interview", action="store_true")
    p.add_argument("--open", action="store_true", help="open the report in a browser")
    p.add_argument("--budget", type=float, default=0.0,
                   help="stop a prebuild once this much USD has been spent")
    args = p.parse_args()
    data_dir = Path(args.data)

    if args.list:
        trajs = pipeline.list_trajectories(data_dir)
        if not trajs:
            print("no trajectories recorded — run with --prebuild first")
            return 1
        print(f"{len(trajs)} recorded run(s):\n")
        for t in trajs:
            print(f"  {t['nct_id']}__{t['npi']}  {t['physician']:<20} "
                  f"{t['recommendation']:<22} {t['score']}/{t['max_score']}  "
                  f"({t['total_seconds']}s live, ${t['total_cost_usd']:.3f})")
        print("\nreplay any of them:  python scripts/run_match.py --replay "
              "--nct <NCT> --npi <NPI>")
        return 0

    if args.prebuild:
        pairs = demo_pairs(data_dir)
        print(f"prebuilding {len(pairs)} pair(s) live on {args.model}\n")
        spent = 0.0
        for nct, npi in pairs:
            if args.budget and spent >= args.budget:
                print(f"\nbudget ${args.budget:.2f} reached — stopping")
                break
            print(f"{nct} × {npi}")
            try:
                traj = pipeline.run(nct, npi, data_dir,
                                    answers=load_answers(data_dir, nct, npi),
                                    model=args.model,
                                    skip_interview=args.no_interview,
                                    emit=printer)
            except Exception as ex:
                print(f"  ✗ {type(ex).__name__}: {ex}\n")
                continue
            spent += traj["total_cost_usd"]
            print(f"  → {traj['recommendation']} · {traj['total_seconds']}s · "
                  f"${traj['total_cost_usd']:.3f} (run total ${spent:.2f})\n")
        print(f"done. spent ${spent:.2f}. Replay with --replay, no key needed.")
        return 0

    if not (args.nct and args.npi):
        print("need --nct and --npi (or --prebuild / --list)")
        return 1

    if args.replay:
        print(f"replaying {args.nct} × {args.npi} (from disk, no API calls)\n")
        traj = pipeline.replay(args.nct, args.npi, data_dir, pace=args.pace,
                               emit=printer)
        print(f"\n  {traj['physician']} × {traj['trial_label']}")
        print(f"  {traj['recommendation']} — {traj['score']}/{traj['max_score']}, "
              f"{traj['coverage']:.0%} coverage")
        print(f"  report: {traj['report_path']}")
        print(f"  (live run took {traj['total_seconds']}s and cost "
              f"${traj['total_cost_usd']:.3f}; this replay cost nothing)")
    else:
        print(f"running {args.nct} × {args.npi} live on {args.model}\n")
        traj = pipeline.run(args.nct, args.npi, data_dir,
                            answers=load_answers(data_dir, args.nct, args.npi),
                            model=args.model,
                            skip_interview=args.no_interview, emit=printer)
        print(f"\n  {traj['recommendation']} — {traj['score']}/{traj['max_score']}")
        print(f"  {traj['total_seconds']}s, ${traj['total_cost_usd']:.3f}")
        print(f"  report: {traj['report_path']}")

    if args.open:
        webbrowser.open(f"file://{(ROOT / traj['report_path']).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
