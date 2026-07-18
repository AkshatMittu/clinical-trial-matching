"""
precedent.py — what happened to trials like this one.

Before deciding whether to join a trial as a site, an investigator wants to know
what similar studies actually did: did they finish, or did they stop early — and
if they stopped, was it because sites could not enrol?

That last number is the one that matters for a site decision, and it is
answerable from public data. ClinicalTrials.gov records a status for every study
and, for terminated ones, a free-text `whyStopped`. Classifying those reasons
turns a pile of statuses into the question a physician is actually asking.

**What this is not.** ClinicalTrials.gov does not report whether a treatment
worked. "Success" here means **operational** success — the trial completed and
enrolled — not efficacy. A trial that completed and showed the drug did nothing
counts as completed. Every summary this module emits says so, because the phrase
"trial success rate" invites exactly the wrong reading.

No LLM: this is a search and a tally.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from . import ctgov
from .ctgov import dig

# Terminal statuses — a trial that reached one of these has an outcome to count.
COMPLETED = {"COMPLETED"}
STOPPED_EARLY = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}
TERMINAL = COMPLETED | STOPPED_EARLY

# Why trials stop. Accrual is first because it is both the most common and the
# one a prospective site can actually affect.
STOP_REASONS = [
    ("accrual", r"accru|enroll|recruit|slow|insufficient particip|"
                r"low participation|lack of (subject|patient|particip)"),
    ("efficacy_or_futility", r"futil|efficac|lack of benefit|did not meet|"
                             r"interim analysis|no significant"),
    ("safety", r"safety|toxicit|adverse|risk|\bdsmb\b|harm"),
    ("business_or_funding", r"business|fund|sponsor decision|financial|budget|"
                            r"strategic|portfolio|company decision"),
    ("superseded", r"superseded|replaced|another (study|trial)|newer"),
    ("covid", r"covid|pandemic"),
]


def classify_stop_reason(text: str) -> str:
    """Bucket a free-text whyStopped. Returns 'unspecified' when it's blank."""
    if not text or not text.strip():
        return "unspecified"
    low = text.lower()
    for label, pattern in STOP_REASONS:
        if re.search(pattern, low):
            return label
    return "other"


def _iv_types(record: dict) -> set[str]:
    ivs = dig(record, "protocolSection", "armsInterventionsModule",
              "interventions", default=[]) or []
    return {(iv.get("type") or "").upper() for iv in ivs if iv.get("type")}


MIN_SAMPLE = 30          # below this, report the rates but flag them as thin


def find_similar(info: dict, limit: int = 400, verbose: bool = True) -> dict:
    """Find terminal-status trials resembling this one, and tally what happened.

    Similarity is intentionally coarse — condition plus phase plus intervention
    type. A tighter filter would return a handful of trials and a percentage
    computed from six studies is noise wearing a decimal point.
    """
    conditions = info.get("conditions") or []
    condition = conditions[0] if conditions else ""
    if not condition:
        return {"available": False, "reason": "trial record has no condition"}

    phases = info.get("phases") or []
    target_ivs = {iv["type"].upper() for iv in (info.get("interventions") or [])
                  if iv.get("type")}

    try:
        studies = list(ctgov.iter_studies(
            condition, tuple(TERMINAL), term="", limit=limit))
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}

    rows = []
    for st in studies:
        ps = st.get("protocolSection", {})
        nct = dig(ps, "identificationModule", "nctId", default="")
        if not nct or nct == info["nct_id"]:
            continue
        status = dig(ps, "statusModule", "overallStatus", default="")
        if status not in TERMINAL:
            continue
        st_phases = dig(ps, "designModule", "phases", default=[]) or []
        if phases and st_phases and not set(phases) & set(st_phases):
            continue
        ivs = _iv_types(st)
        if target_ivs and ivs and not target_ivs & ivs:
            continue

        why = dig(ps, "statusModule", "whyStopped", default="") or ""
        rows.append({
            "nct_id": nct,
            "title": dig(ps, "identificationModule", "briefTitle", default="")[:100],
            "status": status,
            "phases": st_phases,
            "enrollment": dig(ps, "designModule", "enrollmentInfo", "count",
                              default=0),
            "enrollment_type": dig(ps, "designModule", "enrollmentInfo", "type",
                                   default=""),
            "n_sites": len(dig(ps, "contactsLocationsModule", "locations",
                               default=[]) or []),
            "has_results": bool(st.get("hasResults")),
            "why_stopped": why[:240],
            "stop_reason": classify_stop_reason(why) if status in STOPPED_EARLY
            else None,
            "sponsor_class": dig(ps, "sponsorCollaboratorsModule", "leadSponsor",
                                 "class", default=""),
        })

    if not rows:
        return {"available": False, "reason": "no comparable terminal trials found",
                "condition": condition, "phases": phases}

    completed = [r for r in rows if r["status"] in COMPLETED]
    stopped = [r for r in rows if r["status"] in STOPPED_EARLY]
    reasons = Counter(r["stop_reason"] for r in stopped if r["stop_reason"])
    accrual_failures = reasons.get("accrual", 0)

    def _median(vals):
        vals = sorted(v for v in vals if v)
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    return {
        "available": True,
        "query": {"condition": condition, "phases": phases,
                  "intervention_types": sorted(target_ivs), "pool_scanned": len(studies)},
        "n_similar": len(rows),
        "n_completed": len(completed),
        "n_stopped_early": len(stopped),
        "completion_rate": round(len(completed) / len(rows), 3),
        "stopped_early_rate": round(len(stopped) / len(rows), 3),
        "accrual_failure_rate": round(accrual_failures / len(rows), 3),
        "n_accrual_failures": accrual_failures,
        "stop_reasons": dict(reasons.most_common()),
        "results_posted_rate": round(
            sum(1 for r in rows if r["has_results"]) / len(rows), 3),
        "median_enrollment_completed": _median([r["enrollment"] for r in completed]),
        "median_enrollment_stopped": _median([r["enrollment"] for r in stopped]),
        "median_sites_completed": _median([r["n_sites"] for r in completed]),
        "examples_stopped": sorted(
            stopped, key=lambda r: -(r["n_sites"] or 0))[:6],
        "examples_completed": sorted(
            completed, key=lambda r: -(r["n_sites"] or 0))[:4],
        "sample_adequate": len(rows) >= MIN_SAMPLE,
        "sample_note": (None if len(rows) >= MIN_SAMPLE else
                        f"Only {len(rows)} comparable trials found (under "
                        f"{MIN_SAMPLE}). Treat these rates as directional, not "
                        f"statistical — one trial moves them several points."),
        "caveat": ("Operational outcome only. ClinicalTrials.gov records whether "
                   "a trial finished and enrolled, not whether the treatment "
                   "worked — a completed trial with a negative result counts as "
                   "completed. Similarity is condition + phase + intervention "
                   "type, so the comparison set is broad."),
    }


def headline(prec: dict) -> str:
    """One sentence a reader can act on."""
    if not prec.get("available"):
        return f"No precedent available ({prec.get('reason', 'unknown')})."
    return (f"Of {prec['n_similar']} comparable trials that reached a terminal "
            f"status, {prec['completion_rate']:.0%} completed and "
            f"{prec['stopped_early_rate']:.0%} stopped early — "
            f"{prec['accrual_failure_rate']:.0%} of all of them specifically "
            f"for failure to enrol.")


def write(prec: dict, nct_id: str, data_dir: Path) -> Path:
    out_dir = data_dir / "precedent"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{nct_id}.json"
    path.write_text(json.dumps(prec, indent=2))
    return path


def load(nct_id: str, data_dir: Path) -> Optional[dict]:
    path = data_dir / "precedent" / f"{nct_id}.json"
    return json.loads(path.read_text()) if path.exists() else None
