"""
patient.py — a patient-experience proxy for comparable trials.

**No public dataset reports whether trial participants were satisfied.** Nothing
in ClinicalTrials.gov asks patients how they felt. So this module does not
measure satisfaction, and the score it produces is named a proxy everywhere it
appears, because a number labelled "patient satisfaction" would be read as
survey data and it is not.

What the results section *does* record is what patients **did**, which is the
next best thing:

  * **Voluntary withdrawal** — participants counted under "Withdrawal by
    Subject". They chose to leave. This is the single most informative signal
    here: it is a patient's own decision, not a clinical event or a sponsor's.
  * **Retention** — completed vs started. Captures loss for every reason.
  * **Serious adverse events** — participants affected, as a share of those at
    risk. Burden borne rather than preference expressed.
  * **Visit burden** — how often a participant had to attend, from the
    protocol's Schedule of Assessments when one was posted.

The composite is `experience_proxy` (0-100). It is deliberately simple, weighted
arithmetic — no model, no fitting, no hidden calibration — so a reader can
recompute it by hand from the components shown beside it. A composite anyone can
check beats a better one nobody can.

The cohort is the same comparable-trial set `precedent.py` builds, so the two
panels describe the same trials: what happened to the *sites* and what happened
to the *patients*.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import ctgov
from .ctgov import dig

# Contribution of each component to the composite. Voluntary withdrawal carries
# the most because it is the only one reflecting a participant's own choice.
WEIGHTS = {"retention": 0.35, "voluntary_withdrawal": 0.40,
           "serious_ae": 0.15, "visit_burden": 0.10}

# Drop reasons that represent a participant choosing to leave, as opposed to
# being withdrawn for a clinical or administrative reason.
VOLUNTARY = re.compile(r"withdrawal by subject|withdrew consent|patient (choice|"
                       r"decision|preference|request)|subject (choice|decision|"
                       r"request)|voluntary", re.I)


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def participant_flow(record: dict) -> Optional[dict]:
    """Started / completed / voluntary withdrawals from the results section."""
    pf = dig(record, "resultsSection", "participantFlowModule", default={}) or {}
    periods = pf.get("periods") or []
    if not periods:
        return None

    started = completed = 0.0
    voluntary = 0.0
    other_drops = 0.0

    for period in periods[:1]:                    # the overall-study period
        for m in period.get("milestones") or []:
            total = sum(_num(a.get("numSubjects"))
                        for a in (m.get("achievements") or []))
            if m.get("type", "").upper() == "STARTED":
                started = max(started, total)
            elif m.get("type", "").upper() == "COMPLETED":
                completed = max(completed, total)
        for d in period.get("dropWithdraws") or []:
            total = sum(_num(r.get("numSubjects"))
                        for r in (d.get("reasons") or []))
            if VOLUNTARY.search(d.get("type", "")):
                voluntary += total
            else:
                other_drops += total

    if not started:
        return None

    # A trial where NOBODY completed is almost never a retention catastrophe —
    # it is a treat-until-progression design. In metastatic trials patients stay
    # on therapy until the disease progresses, so discontinuation IS the
    # expected endpoint and COMPLETED is recorded as 0. Scoring that as 0%
    # retention would punish the design, not the experience, so retention is
    # marked uninterpretable and dropped from the composite instead.
    progression_driven = completed == 0 and started > 0
    return {
        "started": int(started),
        "completed": int(completed),
        "voluntary_withdrawals": int(voluntary),
        "other_discontinuations": int(other_drops),
        "retention_rate": (None if progression_driven
                           else round(completed / started, 4)),
        "retention_interpretable": not progression_driven,
        "retention_note": ("No participant is recorded as completing — this is "
                           "a treat-until-progression design, where "
                           "discontinuation is the expected endpoint. Retention "
                           "is not meaningful here and is excluded from the "
                           "composite." if progression_driven else None),
        "voluntary_withdrawal_rate": round(voluntary / started, 4),
    }


def serious_ae(record: dict) -> Optional[dict]:
    """Share of at-risk participants with at least one serious adverse event."""
    ae = dig(record, "resultsSection", "adverseEventsModule", default={}) or {}
    groups = ae.get("eventGroups") or []
    if not groups:
        return None
    at_risk = sum(_num(g.get("seriousNumAtRisk")) for g in groups)
    affected = sum(_num(g.get("seriousNumAffected")) for g in groups)
    if not at_risk:
        return None
    return {"serious_at_risk": int(at_risk), "serious_affected": int(affected),
            "serious_ae_rate": round(affected / at_risk, 4)}


def visit_burden(info: Optional[dict]) -> Optional[dict]:
    """Rough visit cadence, from the protocol's visit grid when there is one."""
    if not info:
        return None
    soa = dig(info, "protocol", "schedule_of_assessments", default={}) or {}
    if not soa.get("found"):
        return None
    text = soa.get("text", "")
    # Distinct cycle/week/day column headers approximate the number of visits.
    labels = set(re.findall(r"\b(?:cycle|week|day|visit)\s*\d{1,3}\b", text, re.I))
    return {
        "visit_labels_found": len(labels),
        "confidence": soa.get("confidence"),
        "note": ("Approximated by counting distinct visit-column labels in the "
                 "schedule grid; indicative of cadence, not an exact visit count."),
    }


def _component_scores(flow: Optional[dict], ae: Optional[dict],
                      burden: Optional[dict]) -> tuple[dict, dict]:
    """Each component on 0-100, plus the weights that actually applied."""
    scores: dict[str, float] = {}
    used: dict[str, float] = {}

    if flow and flow.get("retention_rate") is not None:
        scores["retention"] = round(flow["retention_rate"] * 100, 1)
        used["retention"] = WEIGHTS["retention"]
    if flow:
        # 10% voluntary withdrawal is a poor experience; 0% is the ceiling.
        rate = flow["voluntary_withdrawal_rate"]
        scores["voluntary_withdrawal"] = round(max(0.0, 1 - rate / 0.10) * 100, 1)
        used["voluntary_withdrawal"] = WEIGHTS["voluntary_withdrawal"]
    if ae:
        # 50% of participants with a serious AE anchors the floor.
        scores["serious_ae"] = round(
            max(0.0, 1 - ae["serious_ae_rate"] / 0.50) * 100, 1)
        used["serious_ae"] = WEIGHTS["serious_ae"]
    if burden:
        # 40+ distinct visit labels reads as a heavy schedule.
        scores["visit_burden"] = round(
            max(0.0, 1 - burden["visit_labels_found"] / 40) * 100, 1)
        used["visit_burden"] = WEIGHTS["visit_burden"]
    return scores, used


def score_trial(record: dict, info: Optional[dict] = None) -> dict:
    """The proxy for one trial, with every component exposed."""
    flow = participant_flow(record)
    ae = serious_ae(record)
    burden = visit_burden(info)
    scores, used = _component_scores(flow, ae, burden)

    if not scores:
        return {"available": False,
                "reason": "no posted results — participant flow is required"}

    total_w = sum(used.values())
    composite = sum(scores[k] * used[k] for k in scores) / total_w
    return {
        "available": True,
        "experience_proxy": round(composite, 1),
        "components": scores,
        "weights_applied": {k: round(v / total_w, 3) for k, v in used.items()},
        "participant_flow": flow,
        "serious_ae": ae,
        "visit_burden": burden,
        "completeness": f"{len(scores)}/4 components available",
    }


# ----------------------------------------------------------------------------
# Cohort — the same comparable trials precedent.py uses
# ----------------------------------------------------------------------------
def build_cohort(prec: dict, data_dir: Path, limit: int = 12,
                 setting: str = "", verbose: bool = True) -> dict:
    """Score every comparable trial that posted results.

    Only a minority of trials post results, so this cohort is smaller than the
    precedent set and is biased toward trials that finished — the ones that
    stopped early are exactly the ones least likely to have results. The bias is
    reported rather than corrected, because correcting it would mean inventing
    numbers for trials that never published any.
    """
    if not prec.get("available"):
        return {"available": False, "reason": "no precedent cohort to draw from"}

    candidates = [r["nct_id"] for r in
                  (prec.get("examples_completed") or []) +
                  (prec.get("examples_stopped") or [])]
    raw_dir = data_dir / "trials_raw"
    scored, skipped, off_setting = [], 0, 0

    # Retention means different things in different settings, so only compare
    # like with like when the target trial's setting is known.
    manifest_path = data_dir / "trials_manifest.json"
    settings: dict[str, str] = {}
    if setting and manifest_path.exists():
        settings = {r["nct_id"]: r.get("setting", "")
                    for r in json.loads(manifest_path.read_text())}

    for nct in candidates[:limit]:
        if setting and settings.get(nct) and settings[nct] != setting:
            off_setting += 1
            continue
        record = ctgov.load_record(nct, raw_dir)
        if record is None or not record.get("hasResults"):
            skipped += 1
            continue
        s = score_trial(record)
        if not s.get("available"):
            skipped += 1
            continue
        ps = record.get("protocolSection", {})
        scored.append({
            "nct_id": nct,
            "label": (dig(ps, "identificationModule", "acronym", default="")
                      or dig(ps, "identificationModule", "briefTitle",
                             default="")[:60]),
            "status": dig(ps, "statusModule", "overallStatus", default=""),
            "experience_proxy": s["experience_proxy"],
            "components": s["components"],
            "participant_flow": s["participant_flow"],
        })
        if verbose:
            print(f"    {nct}  proxy={s['experience_proxy']:>5}  "
                  f"{s['completeness']}")

    if not scored:
        return {"available": False,
                "reason": f"none of {len(candidates)} comparable trials posted "
                          f"usable results"}

    vals = sorted(t["experience_proxy"] for t in scored)
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return {
        "available": True,
        "n_scored": len(scored),
        "n_skipped_no_results": skipped,
        "n_skipped_other_setting": off_setting,
        "setting": setting or "any",
        "median_proxy": round(median, 1),
        "min_proxy": vals[0],
        "max_proxy": vals[-1],
        "trials": sorted(scored, key=lambda t: -t["experience_proxy"]),
        "weights": WEIGHTS,
        "caveat": ("A PROXY, not satisfaction. No public dataset records how "
                   "trial participants felt. This composes what they DID — "
                   "voluntary withdrawal, retention, serious adverse events, "
                   "visit cadence — into one number. Only trials that posted "
                   "results can be scored, which biases the cohort toward "
                   "trials that finished. Retention is excluded for "
                   "treat-until-progression designs, where discontinuation is "
                   "the expected endpoint rather than a failure."),
    }


def write(cohort: dict, nct_id: str, data_dir: Path) -> Path:
    out_dir = data_dir / "patient_proxy"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{nct_id}.json"
    path.write_text(json.dumps(cohort, indent=2))
    return path


def load(nct_id: str, data_dir: Path) -> Optional[dict]:
    path = data_dir / "patient_proxy" / f"{nct_id}.json"
    return json.loads(path.read_text()) if path.exists() else None
