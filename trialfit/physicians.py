"""
physicians.py — turn trial-record investigator names into identified physicians.

The seed is `official_names` on the trials we collected in step 1. That field is
noisy in three distinct ways, and each one costs us candidates:

  1. Half the entries are corporate placeholders ("Novartis Pharmaceuticals").
  2. NPPES is a **US** registry — European and Japanese investigators, who lead a
     large share of breast trials, simply are not in it.
  3. Common names collide, and a same-name match is not proof of identity.

So the roster is much smaller than the seed list, by design. Every physician on
it is a real, NPI-identified oncologist we can defend.

A `demo_role` marks the pair we build the demo around: one candidate the pipeline
should like, one it should reject. Both must be real people — the negative
control is a genuine physician in the wrong specialty, not a fabrication.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import nppes


@dataclass
class Seed:
    """An investigator name harvested from trial records."""
    name: str
    trials: list[str]          # NCT ids they're listed on

    @property
    def n_trials(self) -> int:
        return len(self.trials)


def harvest_seeds(manifest: list[dict]) -> list[Seed]:
    """Distinct real-person investigator names, most-listed first.

    Appearing on several trials is the one track-record signal available at this
    stage — it costs nothing and correlates with being a genuine trialist.
    """
    counts: Counter = Counter()
    trials: defaultdict = defaultdict(set)
    for row in manifest:
        for raw in (row.get("official_names") or "").split("|"):
            raw = raw.strip()
            if not nppes.is_person(raw):
                continue
            key = nppes.clean_name(raw)
            if key:
                counts[key] += 1
                trials[key].add(row["nct_id"])
    seeds = [Seed(name=n, trials=sorted(trials[n])) for n, _ in counts.most_common()]
    seeds.sort(key=lambda s: (-s.n_trials, s.name))
    return seeds


def resolve_seeds(seeds: list[Seed], limit: int = 40,
                  verbose: bool = True) -> tuple[list[dict], dict]:
    """Resolve seeds against NPPES. Returns (roster, funnel counts)."""
    by_npi: dict[str, dict] = {}
    funnel: Counter = Counter()
    for seed in seeds[:limit]:
        try:
            res = nppes.resolve(seed.name)
        except Exception as e:                       # network hiccup, not fatal
            funnel["error"] += 1
            if verbose:
                print(f"  ! {seed.name}: {type(e).__name__}")
            continue
        funnel[res["status"]] += 1
        if res["status"] != "resolved":
            continue
        person = dict(res["resolved"])
        npi = person["npi"]

        # The NPI is the identity, not the name. "Erica Mayer" and "Erica L.
        # Mayer" are one physician listed two ways; merge their trials rather
        # than carrying a duplicate into the roster.
        if npi in by_npi:
            existing = by_npi[npi]
            merged = sorted(set(existing["source_trials"]) | set(seed.trials))
            existing["source_trials"] = merged
            existing["n_source_trials"] = len(merged)
            existing.setdefault("name_variants", [existing["display_name"]])
            existing["name_variants"].append(seed.name)
            funnel["merged"] += 1
            if verbose:
                print(f"  ~ {seed.name:<30} {npi}  merged into "
                      f"{existing['display_name']}")
            continue

        person.update({
            "display_name": seed.name,
            "source_trials": seed.trials,
            "n_source_trials": seed.n_trials,
            "resolution": "nppes_unique_oncology",
            "demo_role": "",
        })
        by_npi[npi] = person
        if verbose:
            print(f"  ok {seed.name:<30} {npi}  "
                  f"{person['taxonomy'][:34]} · {person['city']}, {person['state']}")

    roster = sorted(by_npi.values(),
                    key=lambda p: (-p["n_source_trials"], p["last_name"]))
    return roster, dict(funnel)


def specialty_of(person: dict) -> str:
    """Coarse specialty label — the axis that most changes which trials fit."""
    blob = " ".join(person.get("all_taxonomies", []) + [person.get("taxonomy", "")]).lower()
    if "surgical oncology" in blob or "surgery" in blob:
        return "surgical_oncology"
    if "radiation oncology" in blob:
        return "radiation_oncology"
    if "gynecologic oncology" in blob:
        return "gynecologic_oncology"
    if "hematology" in blob and "oncology" in blob:
        return "hematology_oncology"
    if "medical oncology" in blob:
        return "medical_oncology"
    return "other"


def pick_demo_physicians(roster: list[dict], gold_ids: set[str],
                         n: int = 3) -> list[dict]:
    """Mark the physicians the demo is built around.

    Two things make a good demo physician:

      * **Gold-trial overlap.** Gold trials have a posted protocol PDF, so the
        requirements built from them come from a real Schedule of Assessments
        rather than being inferred from phase and design. That makes any verdict
        about them defensible.
      * **Specialty spread.** A surgical oncologist and a medical oncologist
        score the same trial differently — a DRUG trial wants one, a PROCEDURE
        trial the other. Picking across specialties is what produces a range of
        scores from real signal instead of a planted mismatch.
    """
    for p in roster:
        p["specialty"] = specialty_of(p)
        p["n_gold_trials"] = len(set(p["source_trials"]) & gold_ids)

    ranked = sorted(roster, key=lambda p: (-p["n_gold_trials"],
                                           -p["n_source_trials"], p["last_name"]))
    picked: list[dict] = []
    seen_specialties: set[str] = set()

    # First pass: the best candidate from each distinct specialty.
    for p in ranked:
        if len(picked) >= n:
            break
        if p["specialty"] in seen_specialties or not p["source_trials"]:
            continue
        seen_specialties.add(p["specialty"])
        picked.append(p)

    # Second pass: fill remaining slots by rank, repeating specialties.
    for p in ranked:
        if len(picked) >= n:
            break
        if p not in picked and p["source_trials"]:
            picked.append(p)

    for p in picked:
        p["demo_role"] = "demo"
        p["demo_trials"] = sorted(set(p["source_trials"]) & gold_ids)
    return roster


def write_roster(roster: list[dict], funnel: dict, data_dir: Path,
                 verbose: bool = True) -> dict:
    """Write per-physician records plus the roster index."""
    phys_dir = data_dir / "physicians"
    phys_dir.mkdir(parents=True, exist_ok=True)
    for p in roster:
        (phys_dir / f"{p['npi']}.json").write_text(json.dumps(p, indent=2))

    (data_dir / "physicians_roster.json").write_text(json.dumps(roster, indent=2))

    demo = [p for p in roster if p.get("demo_role")]
    summary = {
        "n_roster": len(roster),
        "resolution_funnel": funnel,
        "demo": [{"npi": p["npi"], "name": p["display_name"],
                  "specialty": p.get("specialty", ""), "taxonomy": p["taxonomy"],
                  "city": p["city"], "state": p["state"],
                  "n_source_trials": p["n_source_trials"],
                  "gold_trials": p.get("demo_trials", [])}
                 for p in demo],
    }
    (data_dir / "physicians_summary.json").write_text(json.dumps(summary, indent=2))
    if verbose:
        print(f"\n  roster: {len(roster)} physicians -> {data_dir}/physicians_roster.json")
    return summary
