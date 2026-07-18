"""
trialinfo.py — everything about a trial that matching needs.

Step 1's manifest is a selection index: flat, one row per trial, built to answer
"is this good demo material?". Matching needs the opposite — the full text a
requirement can be derived from and cited back to.

This assembles that per trial: eligibility, arms and interventions, outcomes,
sites, officials, dates, and the protocol's Schedule of Assessments.

One judgment call is baked in here, the **eligibility split**. Eligibility text
mixes two different kinds of criteria:

  * *population-defining* — "postmenopausal", "HR+/HER2- early breast cancer".
    These say what patients a practice must already treat, so they bear on
    physician fit.
  * *per-patient screening* — ECOG status, lab thresholds, washout windows.
    These are checked per enrolled patient and say nothing about whether a
    physician is a good site.

Only the first kind should become a requirement. Splitting them is a heuristic
here and the split is recorded, so a later stage can review it rather than
inherit it silently.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import ctgov, protocol
from .ctgov import dig

# Per-patient screening — checked at enrolment, irrelevant to physician fit.
SCREENING_PATTERNS = [
    r"\becog\b", r"karnofsky", r"performance status",
    r"\banc\b", r"absolute neutrophil", r"platelet count", r"hemoglobin",
    r"creatinine", r"bilirubin", r"\bast\b", r"\balt\b", r"transaminase",
    r"\blvef\b", r"ejection fraction", r"\bqtc\b",
    r"washout", r"within \d+ (days|weeks|months) (prior|before)",
    r"pregnan", r"contracept", r"breast ?feeding", r"lactating",
    r"informed consent", r"willing to", r"able to comply", r"life expectancy",
    r"adequate (organ|bone marrow|renal|hepatic|hematologic)",
]

# Population-defining — what the practice must actually contain.
POPULATION_PATTERNS = [
    r"histologically", r"pathologically", r"confirmed", r"diagnos",
    r"stage [0i-v]", r"\bhr[- ]?positive", r"\ber[- ]?positive",
    r"her2[- ]?negative", r"hormone receptor", r"postmenopausal",
    r"premenopausal", r"node[- ]?(positive|negative)", r"tumou?r size",
    r"early[- ]stage", r"adjuvant", r"neoadjuvant", r"metastatic",
    r"prior (therapy|treatment|chemotherapy|endocrine)", r"resect",
]


def split_criteria(text: str) -> list[str]:
    """Break the eligibility blob into individual criterion lines."""
    if not text:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^[\-\*•·]\s*", "", line)
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        if len(line) < 12:                       # headings, stray fragments
            continue
        out.append(line)
    return out


def classify_criterion(line: str) -> str:
    """population_defining / screening / unclear for one criterion."""
    low = line.lower()
    screening = sum(1 for p in SCREENING_PATTERNS if re.search(p, low))
    population = sum(1 for p in POPULATION_PATTERNS if re.search(p, low))
    if screening and screening >= population:
        return "screening"
    if population:
        return "population_defining"
    return "unclear"


def parse_eligibility(text: str) -> dict:
    """Split eligibility into inclusion/exclusion and classify each criterion."""
    if not text:
        return {"available": False, "inclusion": [], "exclusion": [],
                "population_defining": [], "screening": [], "unclear": []}

    low = text.lower()
    i_inc, i_exc = low.find("inclusion"), low.find("exclusion")
    if 0 <= i_inc < i_exc:
        inc_text, exc_text = text[i_inc:i_exc], text[i_exc:]
    elif i_exc >= 0:
        inc_text, exc_text = text[:i_exc], text[i_exc:]
    else:
        inc_text, exc_text = text, ""

    inclusion = split_criteria(inc_text)
    exclusion = split_criteria(exc_text)

    buckets: dict[str, list[dict]] = {"population_defining": [], "screening": [],
                                      "unclear": []}
    for kind, lines in (("inclusion", inclusion), ("exclusion", exclusion)):
        for line in lines:
            buckets[classify_criterion(line)].append({"kind": kind, "text": line})

    return {
        "available": True,
        "raw_chars": len(text),
        "n_inclusion": len(inclusion),
        "n_exclusion": len(exclusion),
        "inclusion": inclusion,
        "exclusion": exclusion,
        "population_defining": buckets["population_defining"],
        "screening": buckets["screening"],
        "unclear": buckets["unclear"],
        "split_note": ("Heuristic split. Only population_defining criteria bear "
                       "on physician fit; screening criteria are per-patient."),
    }


def build(nct_id: str, data_dir: Path, with_protocol: bool = True) -> Optional[dict]:
    """Assemble the full matching-relevant record for one trial."""
    raw_dir = data_dir / "trials_raw"
    record = ctgov.load_record(nct_id, raw_dir)
    if record is None:
        return None

    ps = record.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    elig = ps.get("eligibilityModule", {})
    desc = ps.get("descriptionModule", {})
    contacts = ps.get("contactsLocationsModule", {})
    arms = ps.get("armsInterventionsModule", {})
    outcomes = ps.get("outcomesModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {})

    locations = contacts.get("locations", []) or []
    countries = sorted({l.get("country", "") for l in locations if l.get("country")})
    us_sites = [l for l in locations if l.get("country") == "United States"]

    info = {
        "nct_id": nct_id,
        "acronym": ident.get("acronym", ""),
        "brief_title": ident.get("briefTitle", ""),
        "official_title": ident.get("officialTitle", ""),
        "url": f"https://clinicaltrials.gov/study/{nct_id}",

        "status": status.get("overallStatus", ""),
        "start_date": dig(status, "startDateStruct", "date", default=""),
        "completion_date": dig(status, "completionDateStruct", "date", default=""),

        "phases": design.get("phases", []) or [],
        "study_type": design.get("studyType", ""),
        "allocation": dig(design, "designInfo", "allocation", default=""),
        "masking": dig(design, "designInfo", "maskingInfo", "masking", default=""),
        "enrollment": dig(design, "enrollmentInfo", "count", default=0),

        "lead_sponsor": dig(sponsor, "leadSponsor", "name", default=""),
        "sponsor_class": dig(sponsor, "leadSponsor", "class", default=""),
        "collaborators": [c.get("name", "") for c in
                          (sponsor.get("collaborators") or [])],

        "brief_summary": (desc.get("briefSummary") or "")[:4000],
        "conditions": (ps.get("conditionsModule", {}) or {}).get("conditions", []),

        "eligibility": parse_eligibility(elig.get("eligibilityCriteria", "") or ""),
        "min_age": elig.get("minimumAge", ""),
        "max_age": elig.get("maximumAge", ""),
        "sex": elig.get("sex", ""),
        "healthy_volunteers": elig.get("healthyVolunteers", False),

        "interventions": [{
            "type": iv.get("type", ""),
            "name": iv.get("name", ""),
            "description": (iv.get("description") or "")[:400],
        } for iv in (arms.get("interventions") or [])],
        "arm_groups": [{
            "label": a.get("label", ""),
            "type": a.get("type", ""),
            "description": (a.get("description") or "")[:300],
        } for a in (arms.get("armGroups") or [])],

        "primary_outcomes": [{
            "measure": o.get("measure", ""),
            "timeframe": o.get("timeFrame", ""),
        } for o in (outcomes.get("primaryOutcomes") or [])],
        "n_secondary_outcomes": len(outcomes.get("secondaryOutcomes") or []),

        "n_sites": len(locations),
        "n_us_sites": len(us_sites),
        "countries": countries,
        "sites_sample": [{
            "facility": l.get("facility", ""),
            "city": l.get("city", ""),
            "state": l.get("state", ""),
            "country": l.get("country", ""),
        } for l in locations[:25]],
        "overall_officials": [{
            "name": o.get("name", ""),
            "affiliation": o.get("affiliation", ""),
            "role": o.get("role", ""),
        } for o in (contacts.get("overallOfficials") or [])],
    }

    if with_protocol:
        info["protocol"] = protocol.load(
            nct_id, ctgov.protocol_url(record) or "",
            data_dir / "protocols", data_dir / "cache")
    else:
        info["protocol"] = {"available": False, "reason": "skipped",
                            "burden_source": "inferred"}
    return info


def write(info: dict, data_dir: Path) -> Path:
    out_dir = data_dir / "trial_info"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{info['nct_id']}.json"
    path.write_text(json.dumps(info, indent=2))
    return path
