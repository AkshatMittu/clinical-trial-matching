#!/usr/bin/env python3
"""
Step 3 — gather the information matching needs, on both sides.

    python scripts/collect_evidence.py                # physicians + trials
    python scripts/collect_evidence.py --physicians   # physician evidence only
    python scripts/collect_evidence.py --trials       # trial detail only
    python scripts/collect_evidence.py --no-protocol  # skip PDF download/parse

Physician side -> data/evidence/{NPI}.json   (NPPES, PubMed, CT.gov, CMS x2)
Trial side     -> data/trial_info/{NCT}.json (eligibility split, arms, sites,
                                              Schedule of Assessments)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trialfit import evidence, trialinfo

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description="Collect matching evidence.")
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--physicians", action="store_true", help="physician side only")
    p.add_argument("--trials", action="store_true", help="trial side only")
    p.add_argument("--no-protocol", action="store_true",
                   help="skip protocol PDF download and parsing")
    p.add_argument("--force", action="store_true", help="rebuild existing files")
    args = p.parse_args()

    data_dir = Path(args.data)
    both = not (args.physicians or args.trials)

    roster_path = data_dir / "physicians_roster.json"
    pairs_path = data_dir / "scoring_pairs.json"
    if not roster_path.exists() or not pairs_path.exists():
        print("missing roster or pairs — run scripts/collect_physicians.py first")
        return 1
    roster = json.loads(roster_path.read_text())
    pairs = json.loads(pairs_path.read_text())

    # ---- physician side --------------------------------------------------
    if both or args.physicians:
        people = [p for p in roster if p.get("demo_role")]
        print(f"physician evidence — {len(people)} physicians, 5 sources each")
        for person in people:
            out = data_dir / "evidence" / f"{person['npi']}.json"
            if out.exists() and not args.force:
                print(f"\n  {person['display_name']} — cached, skipping")
                continue
            dossier = evidence.collect(person)
            evidence.write(dossier, data_dir)
            print(f"    coverage        {dossier['coverage']} sources "
                  f"({dossier['elapsed_s']}s)")

    # ---- trial side ------------------------------------------------------
    if both or args.trials:
        ncts = sorted({p["nct_id"] for p in pairs})
        print(f"\ntrial detail — {len(ncts)} distinct trials in the buckets")
        n_protocol = 0
        for nct in ncts:
            out = data_dir / "trial_info" / f"{nct}.json"
            if out.exists() and not args.force:
                info = json.loads(out.read_text())
                n_protocol += bool(info.get("protocol", {}).get("available"))
                print(f"  {nct}  cached")
                continue
            info = trialinfo.build(nct, data_dir,
                                   with_protocol=not args.no_protocol)
            if info is None:
                print(f"  {nct}  ! record unavailable")
                continue
            trialinfo.write(info, data_dir)
            el = info["eligibility"]
            proto = info.get("protocol", {})
            soa = proto.get("schedule_of_assessments", {}) or {}
            n_protocol += bool(proto.get("available"))
            flag = ("SoA p." + str(soa.get("page_number"))) if soa.get("found") \
                else ("protocol, no SoA" if proto.get("available") else "inferred")
            print(f"  {nct}  {el['n_inclusion']:>2}i/{el['n_exclusion']:>2}e  "
                  f"{len(el['population_defining']):>2} population-defining  "
                  f"{info['n_sites']:>3} sites  [{flag}]")
        print(f"\n  {n_protocol}/{len(ncts)} trials grounded in a posted protocol")

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
