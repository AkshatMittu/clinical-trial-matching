"""
nppes.py — NPPES (National Plan & Provider Enumeration System) client.

The registry of every US healthcare provider with an NPI. Public, no API key.
This is how a name on a trial record becomes an identified physician.

The hard part is not fetching — it's **disambiguation**. "Angela DeMichele"
returns a medical oncologist in Philadelphia and a social worker in Ohio. Picking
wrong doesn't fail loudly; it silently attributes one person's evidence to
another. So resolution here refuses to guess: it returns every candidate and a
verdict, and callers must handle `ambiguous` rather than taking the first hit.
"""
from __future__ import annotations

import re
import time
from typing import Iterable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://npiregistry.cms.hhs.gov/api/"
VERSION = "2.1"
POLITE_DELAY = 0.15


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=0.8,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"accept": "application/json"})
    return s


SESSION = _session()

# Taxonomies that plausibly run an oncology trial site.
ONCOLOGY_TAXONOMIES = (
    "medical oncology", "hematology & oncology", "hematology and oncology",
    "radiation oncology", "surgical oncology", "gynecologic oncology",
    "hematology",
)

# Trailing credentials on trial-record names: "Erica Mayer, MD, MPH" -> "Erica Mayer"
_CRED_RE = re.compile(
    r",?\s*\b(m\.?d\.?|d\.?o\.?|ph\.?d\.?|mbbs|mb\s?bch|frcp\w*|facp|faap|"
    r"mph|ms\.?c?|msc|mba|rn|np|pa-?c|bs|ba|dr\.?)\b\.?",
    re.I,
)


def clean_name(name: str) -> str:
    """Strip credentials and honorifics from a trial-record name."""
    n = _CRED_RE.sub("", name or "")
    n = re.sub(r"\s*,\s*$", "", n)
    return re.sub(r"\s+", " ", n).strip(" ,.")


def split_name(name: str) -> tuple[str, str]:
    """(first, last) from a cleaned display name. Middle initials dropped."""
    parts = clean_name(name).split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    first = parts[0]
    last = parts[-1]
    return first, last


def is_person(name: str) -> bool:
    """Filter corporate placeholders out of trial `official_names`.

    Trial records list "Novartis Pharmaceuticals" and "Clinical Trials" in the
    same field as real investigators; roughly half of all entries are these.
    """
    if not name or not name.strip():
        return False
    corp = re.compile(
        r"clinical trial|pharmaceutic|study director|medical monitor|"
        r"\binc\b|\bltd\b|\bllc\b|gmbh|corporation|sponsor|"
        r"hoffmann|novartis|pfizer|astrazeneca|genentech|lilly|roche|merck|"
        r"bristol|amgen|sanofi|bayer|abbvie|gilead|takeda|daiichi|seagen",
        re.I)
    if corp.search(name):
        return False
    # A real investigator entry nearly always carries a clinical credential.
    return bool(re.search(r"\b(MD|DO|PhD|MBBS|MBChB)\b", name, re.I))


# ----------------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------------
def search(last_name: str, first_name: str = "", state: str = "",
           taxonomy: str = "", limit: int = 20) -> list[dict]:
    """Raw NPPES search. Individual providers (NPI-1) only."""
    params = {
        "version": VERSION,
        "enumeration_type": "NPI-1",
        "last_name": last_name,
        "limit": limit,
    }
    if first_name:
        params["first_name"] = first_name
    if state:
        params["state"] = state
    if taxonomy:
        params["taxonomy_description"] = taxonomy
    r = SESSION.get(API, params=params, timeout=30)
    r.raise_for_status()
    time.sleep(POLITE_DELAY)
    body = r.json()
    if body.get("Errors"):
        return []
    return body.get("results", []) or []


def summarize(result: dict) -> dict:
    """Flatten one NPPES result to the fields that matter downstream."""
    basic = result.get("basic", {}) or {}
    taxes = result.get("taxonomies", []) or []
    primary = next((t for t in taxes if t.get("primary")), taxes[0] if taxes else {})
    loc = next((a for a in result.get("addresses", []) or []
                if a.get("address_purpose") == "LOCATION"), {})
    return {
        "npi": result.get("number", ""),
        "first_name": (basic.get("first_name") or "").title(),
        "last_name": (basic.get("last_name") or "").title(),
        "credential": basic.get("credential", "") or "",
        "taxonomy": primary.get("desc") or "",
        "taxonomy_code": primary.get("code") or "",
        "all_taxonomies": [t.get("desc") or "" for t in taxes],
        "city": (loc.get("city") or "").title(),
        "state": loc.get("state", "") or "",
        "org": loc.get("organization_name", "") or "",
        "sole_proprietor": basic.get("sole_proprietor", "") or "",
    }


def is_oncology(candidate: dict) -> bool:
    """Does any of this provider's taxonomies plausibly run an oncology site?"""
    blob = " ".join(candidate.get("all_taxonomies", [])).lower()
    return any(t in blob for t in ONCOLOGY_TAXONOMIES)


# ----------------------------------------------------------------------------
# Resolution — name -> one identified physician, or an honest refusal
# ----------------------------------------------------------------------------
def resolve(name: str, state: str = "", require_oncology: bool = True) -> dict:
    """Resolve a display name to a single NPI.

    Returns {status, candidates, resolved}. Status is one of:
      resolved   exactly one plausible match — `resolved` holds it
      ambiguous  several plausible matches — caller must NOT pick one
      not_found  nothing matched

    Narrowing runs in order (full name -> +state -> oncology filter) and stops
    as soon as one candidate remains. If several survive every filter the answer
    is `ambiguous`, deliberately: a wrong pick misattributes evidence silently.
    """
    first, last = split_name(name)
    if not last:
        return {"status": "not_found", "query": name, "candidates": [], "resolved": None}

    raw = search(last, first, state=state)
    candidates = [summarize(r) for r in raw]
    if not candidates and state:                     # state may be stale — retry wide
        candidates = [summarize(r) for r in search(last, first)]
    if not candidates:
        return {"status": "not_found", "query": name, "candidates": [], "resolved": None}

    pool = candidates
    if require_oncology:
        pool = [c for c in pool if is_oncology(c)]
        if not pool:
            # A lone same-name match is NOT proof of identity. Without an
            # oncology taxonomy this is a name collision, not our investigator —
            # "George Thomas Budd" otherwise resolves to a dentist in NJ.
            return {"status": "not_found", "query": name,
                    "candidates": candidates, "resolved": None,
                    "note": "no oncology taxonomy among candidates"}

    if len(pool) == 1:
        return {"status": "resolved", "query": name,
                "candidates": candidates, "resolved": pool[0]}
    return {"status": "ambiguous", "query": name,
            "candidates": candidates, "resolved": None,
            "note": f"{len(pool)} plausible candidates after filtering"}
