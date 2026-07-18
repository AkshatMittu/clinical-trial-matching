"""
evidence.py — assemble everything public data can say about one physician.

Five sources, each answering a different question:

    NPPES            who they are, and what they're licensed as
    PubMed           do they publish in this disease area, and recently
    ClinicalTrials   have they held investigator roles before
    Open Payments    has industry paid them for research before
    Medicare         how much clinical volume can we see

This module is a **collector, not a judge.** It records what each source
returned, including when a source returned nothing, and never forms a view on
fit. Later stages do the judging, and they can only cite what appears here.

Every finding carries a `ref` that resolves to a public record — `PMID:41812623`,
`NCT02513394`, `OpenPayments:1040131905`. A claim with no ref is not evidence.

The gap this cannot close: public data is good at *"is this person a
researcher"* and bad at *"does this person treat THIS patient population"*.
Medicare volume is the only population signal available and it's a weak proxy.
That gap is recorded explicitly in `gaps` rather than papered over.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from . import cms, ctgov, nppes, pubmed

STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_UNAVAILABLE = "source_unavailable"


def _entry(source: str, status: str, summary: str, records: list,
           **extra) -> dict:
    return {"source": source, "status": status, "summary": summary,
            "n_records": len(records), "records": records, **extra}


# ----------------------------------------------------------------------------
# Prior investigator roles
# ----------------------------------------------------------------------------
def investigator_roles(name: str, condition: str = "", max_results: int = 20,
                       exclude: Optional[set[str]] = None) -> dict:
    """Trials where this person appears as an official.

    ClinicalTrials.gov has no investigator index — this is a full-text search, so
    a hit means the name appears *somewhere* in the record. We separate
    `confirmed` (listed in overallOfficials) from `mentioned` (matched the text
    only), because the second is weak evidence and shouldn't be counted as
    though it were the first.
    """
    exclude = exclude or set()
    cleaned = nppes.clean_name(name)
    last = cleaned.split()[-1] if cleaned else ""
    try:
        params = {"query.term": f'"{cleaned}"', "pageSize": max_results,
                  "countTotal": "true"}
        if condition:
            params["query.cond"] = condition
        body = ctgov._get(ctgov.API, params)
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}",
                "confirmed": [], "mentioned": []}

    confirmed, mentioned = [], []
    for study in body.get("studies", []) or []:
        ps = study.get("protocolSection", {})
        nct = ctgov.dig(ps, "identificationModule", "nctId", default="")
        if not nct or nct in exclude:
            continue
        officials = ctgov.dig(ps, "contactsLocationsModule", "overallOfficials",
                              default=[]) or []
        names = " | ".join(o.get("name", "") for o in officials)
        row = {
            "ref": nct,
            "nct_id": nct,
            "title": ctgov.dig(ps, "identificationModule", "briefTitle",
                               default="")[:110],
            "status": ctgov.dig(ps, "statusModule", "overallStatus", default=""),
            "phases": ctgov.dig(ps, "designModule", "phases", default=[]),
            "officials": names[:200],
        }
        if last and last.lower() in names.lower():
            confirmed.append(row)
        else:
            mentioned.append(row)

    return {"available": True, "query": cleaned,
            "total_hits": int(body.get("totalCount", 0) or 0),
            "confirmed": confirmed, "mentioned": mentioned}


# ----------------------------------------------------------------------------
# Dossier
# ----------------------------------------------------------------------------
def collect(physician: dict, topic: str = "breast neoplasms[MeSH Terms]",
            condition: str = "breast cancer", verbose: bool = True) -> dict:
    """Gather every public signal for one physician."""
    npi = physician["npi"]
    first, last = physician.get("first_name", ""), physician.get("last_name", "")
    name = physician.get("display_name") or f"{first} {last}"
    started = time.time()
    entries: list[dict] = []
    gaps: list[str] = []

    if verbose:
        print(f"\n  {name} (NPI {npi})")

    # --- identity ----------------------------------------------------------
    entries.append(_entry(
        "NPPES", STATUS_FOUND,
        f"{physician.get('taxonomy', '')} in "
        f"{physician.get('city', '')}, {physician.get('state', '')}",
        [{"ref": f"NPI:{npi}", "npi": npi,
          "taxonomy": physician.get("taxonomy", ""),
          "all_taxonomies": physician.get("all_taxonomies", []),
          "city": physician.get("city", ""),
          "state": physician.get("state", "")}],
        specialty=physician.get("specialty", "")))

    # --- publications ------------------------------------------------------
    pubs = pubmed.search(first, last, topic=topic, max_results=25)
    recent = pubmed.search(first, last, topic=topic, recent_years=5, max_results=5)
    if not pubs.get("available"):
        entries.append(_entry("PubMed", STATUS_UNAVAILABLE,
                              pubs.get("error", "search failed"), []))
    elif pubs["count"] == 0:
        entries.append(_entry("PubMed", STATUS_NOT_FOUND,
                              f"no publications for {pubs['query']} in topic", []))
    else:
        entries.append(_entry(
            "PubMed", STATUS_FOUND,
            f"{pubs['count']} publications in topic "
            f"({recent.get('count', 0)} in the last 5 years)",
            pubs["records"], query=pubs["query"],
            total=pubs["count"], recent_5y=recent.get("count", 0),
            caveat="Author search matches surname+initial; may include namesakes."))
    if verbose:
        print(f"    PubMed          {pubs.get('count', 0)} pubs "
              f"({recent.get('count', 0)} recent)")

    # --- prior investigator roles -----------------------------------------
    roles = investigator_roles(name, condition=condition)
    if not roles.get("available"):
        entries.append(_entry("ClinicalTrials.gov", STATUS_UNAVAILABLE,
                              roles.get("reason", "search failed"), []))
    else:
        confirmed = roles["confirmed"]
        entries.append(_entry(
            "ClinicalTrials.gov",
            STATUS_FOUND if confirmed else STATUS_NOT_FOUND,
            f"{len(confirmed)} trials list this name as an overall official "
            f"({len(roles['mentioned'])} weaker text-only mentions)",
            confirmed, mentioned=roles["mentioned"][:10],
            caveat="Full-text search; 'mentioned' rows are weak evidence."))
    if verbose:
        print(f"    CT.gov roles    {len(roles.get('confirmed', []))} confirmed, "
              f"{len(roles.get('mentioned', []))} mentioned")

    # --- industry research payments ---------------------------------------
    pay = cms.open_payments(npi)
    if not pay.get("available"):
        entries.append(_entry("CMS Open Payments", STATUS_UNAVAILABLE,
                              pay.get("reason", "unavailable"), []))
        gaps.append("industry research-payment history unverified")
    elif pay["count"] == 0:
        entries.append(_entry("CMS Open Payments", STATUS_NOT_FOUND,
                              f"no research payments in {pay['program_year']}", []))
    else:
        entries.append(_entry(
            "CMS Open Payments", STATUS_FOUND,
            f"${pay['total_usd']:,.0f} across {pay['count']} research payments "
            f"from {pay['n_payers']} sponsors ({pay['program_year']})",
            pay["records"], program_year=pay["program_year"],
            payers=pay["payers"], studies=pay["studies"]))
    if verbose:
        print(f"    Open Payments   {pay.get('count', 0)} records"
              + (f", ${pay['total_usd']:,.0f}" if pay.get("available") else
                 f" ({pay.get('reason', '')[:40]})"))

    # --- clinical volume ---------------------------------------------------
    vol = cms.medicare_volume(npi)
    if not vol.get("available"):
        entries.append(_entry("CMS Medicare", STATUS_UNAVAILABLE,
                              vol.get("reason", "unavailable"), []))
        gaps.append("practice volume unverified")
    elif not vol.get("found"):
        entries.append(_entry("CMS Medicare", STATUS_NOT_FOUND,
                              "no Medicare claim lines for this NPI", []))
        gaps.append("practice volume unverified (no Medicare claims)")
    else:
        entries.append(_entry(
            "CMS Medicare", STATUS_FOUND,
            f"{vol['total_services']:,.0f} services for "
            f"{vol['total_beneficiaries']:,.0f} beneficiaries across "
            f"{vol['n_hcpcs_codes']} codes; listed as {vol['provider_type']}",
            vol["records"], provider_type=vol["provider_type"],
            total_services=vol["total_services"],
            total_beneficiaries=vol["total_beneficiaries"],
            caveat=vol["caveat"]))
    if verbose:
        print(f"    Medicare        "
              + (f"{vol['total_services']:,.0f} services, "
                 f"{vol['n_hcpcs_codes']} codes" if vol.get("found")
                 else vol.get("reason", "not found")[:40]))

    # The gap public data structurally cannot close.
    gaps.append("patient-population fit (does this physician treat THIS "
                "population, at what volume) is not verifiable from public data")

    found = [e for e in entries if e["status"] == STATUS_FOUND]
    return {
        "npi": npi,
        "physician": name,
        "specialty": physician.get("specialty", ""),
        "taxonomy": physician.get("taxonomy", ""),
        "city": physician.get("city", ""),
        "state": physician.get("state", ""),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(time.time() - started, 1),
        "sources_queried": [e["source"] for e in entries],
        "sources_with_data": [e["source"] for e in found],
        "coverage": f"{len(found)}/{len(entries)}",
        "entries": entries,
        "gaps": gaps,
        "note": ("Collector only — no fit judgments here. Every record carries a "
                 "public ref."),
    }


def write(dossier: dict, data_dir: Path) -> Path:
    out_dir = data_dir / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{dossier['npi']}.json"
    path.write_text(json.dumps(dossier, indent=2))
    return path
