"""
buckets.py — for each physician, a bucket of trials to score against.

The pipeline is physician-centric: a physician is the subject, and their bucket
is the set of trials we ask "would this person be a good site investigator?"

A bucket is deliberately **graded**. If every trial in it were an obvious fit,
the resulting scores would all cluster high and tell us nothing about whether the
scorer works. So each trial is assigned an expected tier from public signals
before any scoring happens:

    known      this physician is a listed official on the trial
    strong     right subtype, right setting, specialty matches the interventions
    moderate   in-scope but one axis is off
    weak       off-subtype, wrong setting, or specialty mismatch

The tier is a **prediction, not an input.** Nothing downstream reads it while
scoring. It exists so that afterwards we can ask whether the scores actually
separate the tiers — if `weak` trials score as high as `known` ones, the scorer
is not measuring fit, and we want to find that out from the data rather than
assume it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Which intervention types each specialty is plausibly the investigator for.
SPECIALTY_INTERVENTIONS = {
    "medical_oncology": {"DRUG", "BIOLOGICAL"},
    "hematology_oncology": {"DRUG", "BIOLOGICAL"},
    "surgical_oncology": {"PROCEDURE", "RADIATION", "DEVICE"},
    "radiation_oncology": {"RADIATION", "DEVICE"},
    "gynecologic_oncology": {"DRUG", "PROCEDURE", "BIOLOGICAL"},
}

TIER_ORDER = ["known", "strong", "moderate", "weak"]

# Roughly what we'd expect a working scorer to produce, for later comparison.
TIER_EXPECTED_SCORE = {
    "known": "high", "strong": "high", "moderate": "mid", "weak": "low",
}


def specialty_alignment(specialty: str, intervention_types: str) -> float:
    """0.0-1.0: how well this physician's specialty matches what the trial does."""
    wanted = SPECIALTY_INTERVENTIONS.get(specialty, set())
    present = {t for t in (intervention_types or "").split("|") if t}
    if not wanted or not present:
        return 0.5                                    # unknown, not a mismatch
    overlap = wanted & present
    if not overlap:
        return 0.0
    return len(overlap) / len(present)


def assign_tier(physician: dict, trial: dict) -> tuple[str, list[str]]:
    """Expected-fit tier for one (physician, trial) pair, plus the reasons why."""
    reasons: list[str] = []

    if trial["nct_id"] in physician.get("source_trials", []):
        return "known", ["listed as an overall official on this trial"]

    align = specialty_alignment(physician.get("specialty", ""),
                                trial.get("intervention_types", ""))
    in_scope = bool(trial.get("in_scope"))
    setting = trial.get("setting", "unspecified")
    early = setting in ("early_adjuvant", "mixed")

    if in_scope:
        reasons.append("subtype in scope (HR+/HER2-)")
    else:
        reasons.append("subtype out of scope")
    reasons.append(f"setting: {setting}")

    if align >= 0.5:
        reasons.append(f"specialty matches interventions ({align:.0%})")
    elif align == 0.0:
        reasons.append("specialty does not match interventions")
    else:
        reasons.append(f"partial specialty match ({align:.0%})")

    if trial.get("has_protocol"):
        reasons.append("protocol PDF available")

    if in_scope and early and align >= 0.5:
        tier = "strong"
    elif in_scope and (early or align >= 0.5):
        tier = "moderate"
    elif in_scope or align >= 0.5:
        tier = "moderate" if align > 0.0 else "weak"
    else:
        tier = "weak"
    return tier, reasons


def build_bucket(physician: dict, manifest: list[dict], per_tier: int = 3,
                 prefer_protocol: bool = True) -> dict:
    """Assemble one physician's graded trial bucket.

    Takes up to `per_tier` trials from each tier so the bucket spans the range
    rather than filling up with whichever tier happens to be most common.
    """
    tiered: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}

    for trial in manifest:
        if trial.get("study_type") != "INTERVENTIONAL":
            continue
        tier, reasons = assign_tier(physician, trial)
        tiered[tier].append({
            "nct_id": trial["nct_id"],
            "label": trial.get("acronym") or trial.get("brief_title", "")[:60],
            "tier": tier,
            "expected_score": TIER_EXPECTED_SCORE[tier],
            "reasons": reasons,
            "setting": trial.get("setting", ""),
            "in_scope": bool(trial.get("in_scope")),
            "has_protocol": bool(trial.get("has_protocol")),
            "quality_score": trial.get("quality_score", 0),
            "intervention_types": trial.get("intervention_types", ""),
            "trial_bucket": trial.get("bucket", ""),
        })

    selected: list[dict] = []
    for tier in TIER_ORDER:
        rows = tiered[tier]
        # Prefer trials with a protocol PDF — they ground the requirements side.
        rows.sort(key=lambda r: (not (prefer_protocol and r["has_protocol"]),
                                 -r["quality_score"], r["nct_id"]))
        take = len(rows) if tier == "known" else per_tier
        selected.extend(rows[:take])

    counts = {t: sum(1 for r in selected if r["tier"] == t) for t in TIER_ORDER}
    return {
        "npi": physician["npi"],
        "physician": physician["display_name"],
        "specialty": physician.get("specialty", ""),
        "taxonomy": physician.get("taxonomy", ""),
        "city": physician.get("city", ""),
        "state": physician.get("state", ""),
        "tier_counts": counts,
        "n_trials": len(selected),
        "trials": selected,
    }


def build_all(roster: list[dict], manifest: list[dict], per_tier: int = 3,
              demo_only: bool = True) -> list[dict]:
    people = [p for p in roster if p.get("demo_role")] if demo_only else roster
    return [build_bucket(p, manifest, per_tier=per_tier) for p in people]


def write_buckets(buckets: list[dict], data_dir: Path,
                  verbose: bool = True) -> dict:
    """Write per-physician buckets plus a flat pair list for the scorer."""
    out_dir = data_dir / "buckets"
    out_dir.mkdir(parents=True, exist_ok=True)
    for b in buckets:
        (out_dir / f"{b['npi']}.json").write_text(json.dumps(b, indent=2))

    # The flat work list every later stage iterates over.
    pairs = [{"npi": b["npi"], "physician": b["physician"], "nct_id": t["nct_id"],
              "label": t["label"], "tier": t["tier"],
              "expected_score": t["expected_score"]}
             for b in buckets for t in b["trials"]]
    (data_dir / "scoring_pairs.json").write_text(json.dumps(pairs, indent=2))

    summary = {
        "n_physicians": len(buckets),
        "n_pairs": len(pairs),
        "tier_totals": {t: sum(1 for p in pairs if p["tier"] == t)
                        for t in TIER_ORDER},
        "physicians": [{"npi": b["npi"], "name": b["physician"],
                        "specialty": b["specialty"],
                        "tier_counts": b["tier_counts"]} for b in buckets],
    }
    (data_dir / "buckets_summary.json").write_text(json.dumps(summary, indent=2))
    if verbose:
        print(f"\n  {len(pairs)} pairs across {len(buckets)} physicians "
              f"-> {data_dir}/scoring_pairs.json")
    return summary
