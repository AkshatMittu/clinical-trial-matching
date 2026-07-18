"""
collect.py — build a demoable sample of clinical trials from ClinicalTrials.gov.

Pulls candidate trials for a scope, flattens the metadata that decides whether a
trial is good demo material, scores it, and sorts it into buckets.

What makes a trial good demo material, in priority order:

  1. A posted protocol PDF. The Schedule of Assessments inside it is the only
     honest source of operational burden — what a site actually has to do per
     visit. Without it, burden has to be guessed from phase/design.
  2. Rich eligibility text. Thin criteria produce thin, generic requirements.
  3. Named overall officials. These are real investigators, the seed for the
     physician side of the pipeline.
  4. Scale — many sites, real enrollment. Signals a trial that needed a genuine
     site network rather than one academic center.

Scope is a parameter, not a constant. `SCOPES` holds ready-made presets; pass
`--scope` on the CLI or build your own `Scope`.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from . import ctgov
from .ctgov import dig

# ----------------------------------------------------------------------------
# Scope — what slice of ClinicalTrials.gov to pull
# ----------------------------------------------------------------------------
@dataclass
class Scope:
    """One searchable slice of CT.gov, plus how to tag trials inside it."""
    name: str
    condition: str                              # query.cond
    focused_term: str = ""                      # Essie full-text narrowing query
    # keyword -> substrings; each becomes a boolean column on the manifest
    tags: dict[str, list[str]] = field(default_factory=dict)
    # tags that must ALL be true for `in_scope` (the tight subtype match)
    require_tags: tuple[str, ...] = ()
    # trials we always want present regardless of score: nct -> label
    anchors: dict[str, str] = field(default_factory=dict)

    statuses: tuple[str, ...] = ("RECRUITING", "ACTIVE_NOT_RECRUITING",
                                 "COMPLETED", "TERMINATED")
    keep_phases: tuple[str, ...] = ("PHASE2", "PHASE3")
    keep_study_type: str = "INTERVENTIONAL"


BREAST_HR_POS = Scope(
    name="breast_hr_pos",
    condition="breast cancer",
    focused_term=(
        '(adjuvant OR "early breast" OR "early-stage" OR operable) '
        'AND ("HR-positive" OR "ER-positive" OR "hormone receptor positive" '
        'OR "estrogen receptor positive") AND ("HER2-negative" OR "HER2 negative")'
    ),
    tags={
        "hr_pos": ["er-positive", "er positive", "estrogen receptor positive",
                   "hr-positive", "hr positive", "hormone receptor positive",
                   "er+", "hr+", "pr-positive", "pr+"],
        "her2_neg": ["her2-negative", "her2 negative", "her2-", "her-2 negative",
                     "her2 -", "her2negative"],
        "early_adjuvant": ["early breast", "early-stage", "adjuvant", "neoadjuvant",
                           "operable", "non-metastatic", "stage i", "stage ii",
                           "stage iii"],
        "metastatic": ["metastatic", "stage iv", "advanced breast", " mbc"],
    },
    require_tags=("hr_pos", "her2_neg", "early_adjuvant"),
    anchors={"NCT05952557": "CAMBRIA-2", "NCT02513394": "PALLAS"},
)

SCOPES = {s.name: s for s in (BREAST_HR_POS,)}


@dataclass
class Limits:
    max_records: int = 400       # candidates pulled from the API
    n_gold: int = 40             # protocol-grounded set
    n_demo: int = 15             # recruiting set
    n_broad: int = 250           # breadth, for mining investigators


# ----------------------------------------------------------------------------
# Metadata extraction — one flat row per trial
# ----------------------------------------------------------------------------
def tag_text(text: str, scope: Scope) -> dict:
    """Keyword-tag a trial's text. Heuristic — confirm borderline cases by hand."""
    low = text.lower()
    flags = {k: any(kw in low for kw in kws) for k, kws in scope.tags.items()}
    flags["in_scope"] = all(flags.get(t, False) for t in scope.require_tags)
    return flags


def count_criteria(text: str) -> tuple[int, int]:
    """Rough inclusion / exclusion bullet counts.

    CT.gov returns eligibility as ONE free-text blob with inclusion and
    exclusion mixed together. These counts are a richness proxy only; real
    parsing happens downstream against the full text.
    """
    if not text:
        return 0, 0
    low = text.lower()
    inc, exc = low.find("inclusion"), low.find("exclusion")
    inc_block = text[inc:exc] if 0 <= inc < exc else (text[inc:] if inc >= 0 else "")
    exc_block = text[exc:] if exc >= 0 else ""

    def bullets(s: str) -> int:
        n = 0
        for line in s.splitlines():
            stripped = line.strip()
            if stripped.startswith(("-", "*", "•")) or stripped[:2].strip().isdigit():
                n += 1
        return n

    return bullets(inc_block), bullets(exc_block)


def extract_meta(record: dict, scope: Scope) -> dict:
    """Flatten one v2 record into the fields that drive selection."""
    ps = record.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    elig = ps.get("eligibilityModule", {})
    conds = ps.get("conditionsModule", {})
    contacts = ps.get("contactsLocationsModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {})
    desc = ps.get("descriptionModule", {})

    nct = ident.get("nctId", "")
    elig_text = elig.get("eligibilityCriteria", "") or ""
    officials = contacts.get("overallOfficials", []) or []
    locations = contacts.get("locations", []) or []
    conditions = conds.get("conditions", []) or []

    # Tag against everything the trial says about itself, not just the title.
    taggable = " ".join([
        ident.get("briefTitle", "") or "",
        ident.get("officialTitle", "") or "",
        desc.get("briefSummary", "") or "",
        elig_text,
        " ".join(conditions),
    ])

    inc, exc = count_criteria(elig_text)
    purl = ctgov.protocol_url(record)

    meta = {
        "nct_id": nct,
        "acronym": ident.get("acronym", ""),
        "brief_title": ident.get("briefTitle", ""),
        "is_anchor": nct in scope.anchors,
        "anchor_label": scope.anchors.get(nct, ""),

        "overall_status": status.get("overallStatus", ""),
        "phases": "|".join(design.get("phases", []) or []),
        "study_type": design.get("studyType", ""),
        "enrollment": dig(design, "enrollmentInfo", "count", default=0),

        "lead_sponsor": dig(sponsor, "leadSponsor", "name", default=""),
        "sponsor_class": dig(sponsor, "leadSponsor", "class", default=""),

        "elig_chars": len(elig_text),
        "elig_inclusion": inc,
        "elig_exclusion": exc,
        "min_age": elig.get("minimumAge", ""),
        "sex": elig.get("sex", ""),

        "n_locations": len(locations),
        "n_officials": len(officials),
        "official_names": "|".join(o.get("name", "") for o in officials),

        "has_protocol": bool(purl),
        "protocol_url": purl or "",
        "has_results": bool(record.get("hasResults")),

        "conditions": "|".join(conditions),
    }
    meta.update(tag_text(taggable, scope))
    return meta


# ----------------------------------------------------------------------------
# Scoring & bucketing
# ----------------------------------------------------------------------------
def quality_score(m: dict) -> float:
    """Composite richness score. Protocol PDF dominates — it's the grounding."""
    s = 0.0
    if m["has_protocol"]:
        s += 3.0
    if m["has_results"]:
        s += 1.0
    if m["elig_chars"] > 1500:
        s += 2.0
    elif m["elig_chars"] > 600:
        s += 1.0
    if m["n_officials"] >= 1:
        s += 1.0
    if m["n_locations"] >= 10:
        s += 1.0
    elif m["n_locations"] >= 1:
        s += 0.5
    if (m["enrollment"] or 0) >= 100:
        s += 1.0
    if "PHASE3" in m["phases"]:
        s += 1.0
    elif "PHASE2" in m["phases"]:
        s += 0.5
    return round(s, 2)


def phase_ok(m: dict, scope: Scope) -> bool:
    return any(p in m["phases"] for p in scope.keep_phases)


def assign_bucket(m: dict, scope: Scope) -> str:
    if m["study_type"] != scope.keep_study_type or not phase_ok(m, scope):
        return "other"
    if m["in_scope"] and m["has_protocol"] and m["elig_chars"] > 600:
        return "gold"
    if m["in_scope"] and m["overall_status"] == "RECRUITING":
        return "demo"
    if m["in_scope"] or m.get("metastatic"):
        return "broad"
    return "other"


# ----------------------------------------------------------------------------
# Collection
# ----------------------------------------------------------------------------
def recon(scope: Scope) -> dict:
    """How big is each layer of the funnel, before pulling anything."""
    broad = ctgov.count(scope.condition, scope.statuses)
    focused = (ctgov.count(scope.condition, scope.statuses, scope.focused_term)
               if scope.focused_term else broad)
    return {"condition": scope.condition, "broad_total": broad,
            "focused_total": focused}


def collect(scope: Scope, limits: Limits, data_dir: Path,
            use_focused: bool = True, verbose: bool = True) -> list[dict]:
    """Pull candidates, cache raw records, return scored+bucketed metadata."""
    raw_dir = data_dir / "trials_raw"
    term = scope.focused_term if use_focused else ""

    metas: list[dict] = []
    seen: set[str] = set()

    for record in ctgov.iter_studies(scope.condition, scope.statuses, term,
                                     limits.max_records):
        m = extract_meta(record, scope)
        if not m["nct_id"] or m["nct_id"] in seen:
            continue
        seen.add(m["nct_id"])
        m["quality_score"] = quality_score(m)
        m["bucket"] = assign_bucket(m, scope)
        metas.append(m)
        ctgov.save_record(record, raw_dir)
        if verbose and len(metas) % 50 == 0:
            print(f"  ... {len(metas)} pulled")

    # Anchors are prototype trials we always want on hand, score regardless.
    for nct, label in scope.anchors.items():
        if nct in seen:
            continue
        record = ctgov.load_record(nct, raw_dir)
        if record is None:
            if verbose:
                print(f"  ! anchor {nct} ({label}) not retrievable")
            continue
        m = extract_meta(record, scope)
        m["quality_score"] = quality_score(m)
        m["bucket"] = assign_bucket(m, scope)
        metas.append(m)
        seen.add(nct)
        if verbose:
            print(f"  + anchor {nct} ({label}) added, bucket={m['bucket']}")

    metas.sort(key=lambda m: (-m["quality_score"], m["nct_id"]))
    return metas


def write_outputs(metas: list[dict], scope: Scope, limits: Limits,
                  data_dir: Path, verbose: bool = True) -> dict:
    """Write the manifest (CSV + JSON) and the per-bucket id lists."""
    data_dir.mkdir(parents=True, exist_ok=True)

    if metas:
        cols = list(metas[0].keys())
        with (data_dir / "trials_manifest.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(metas)
        (data_dir / "trials_manifest.json").write_text(json.dumps(metas, indent=2))

    caps = {"gold": limits.n_gold, "demo": limits.n_demo, "broad": limits.n_broad}
    counts = {}
    for bucket, cap in caps.items():
        # Anchors first, then by score — an anchor never falls off the list.
        rows = [m for m in metas if m["bucket"] == bucket]
        rows.sort(key=lambda m: (not m["is_anchor"], -m["quality_score"]))
        ids = [m["nct_id"] for m in rows[:cap]]
        (data_dir / f"trials_{bucket}_ids.txt").write_text("\n".join(ids) + "\n")
        counts[bucket] = len(ids)

    counts["other"] = sum(1 for m in metas if m["bucket"] == "other")
    counts["total"] = len(metas)

    summary = {"scope": scope.name, "condition": scope.condition,
               "buckets": counts,
               "anchors_present": [m["nct_id"] for m in metas if m["is_anchor"]]}
    (data_dir / "collection_summary.json").write_text(json.dumps(summary, indent=2))

    if verbose:
        print(f"\n  manifest: {len(metas)} trials -> {data_dir}/trials_manifest.csv")
        for b in ("gold", "demo", "broad", "other"):
            print(f"    {b:6s} {counts.get(b, 0):4d}")
    return summary
