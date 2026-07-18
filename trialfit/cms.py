"""
cms.py — CMS public data: Open Payments and Medicare utilization.

Two signals about a physician that nothing else provides:

  * **Open Payments research payments** — direct evidence that industry has paid
    this person for *research* before. The closest public proxy for "has run an
    industry-sponsored trial", and the payments name the study.
  * **Medicare utilization** — procedure and beneficiary counts, a floor on
    practice volume.

Both dataset ids change with each program year. Rather than pinning them in
config and going stale (the previous version of this project needed two
hand-refreshed UUIDs in `.env`), both are **discovered at runtime** from the CMS
metastore and cached for the process.

Every function degrades to `{"available": False, ...}` instead of raising, so a
CMS outage downgrades one piece of evidence rather than failing a whole run.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

OPENPAYMENTS = "https://openpaymentsdata.cms.gov/api/1"
DATA_CMS = "https://data.cms.gov"
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
_CACHE: dict[str, object] = {}


def _get(url: str, params: dict | None = None, timeout: int = 90):
    r = SESSION.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    time.sleep(POLITE_DELAY)
    return r.json()


# ----------------------------------------------------------------------------
# Dataset discovery
# ----------------------------------------------------------------------------
def research_dataset(year: Optional[int] = None) -> Optional[dict]:
    """Newest queryable 'Research Payment Data' distribution.

    Only some program years are indexed in the datastore; the rest are
    download-only CSVs. Walk newest-first and return the first that answers.
    """
    key = f"op_research_{year}"
    if key in _CACHE:
        return _CACHE[key]                                   # type: ignore[return-value]

    try:
        items = _get(f"{OPENPAYMENTS}/metastore/schemas/dataset/items",
                     {"show-reference-ids": "true"})
    except Exception:
        return None

    found = []
    for item in items:
        title = item.get("title", "") or ""
        if "research payment" not in title.lower():
            continue
        m = re.search(r"(20\d\d)", title)
        if not m:
            continue
        yr = int(m.group(1))
        if year and yr != year:
            continue
        dists = item.get("distribution") or []
        dist_id = next((d.get("identifier") for d in dists
                        if isinstance(d, dict) and d.get("identifier")), None)
        if dist_id:
            found.append({"year": yr, "distribution_id": dist_id, "title": title})

    for cand in sorted(found, key=lambda c: -c["year"]):
        try:                                     # confirm it's actually indexed
            _get(f"{OPENPAYMENTS}/datastore/imports/{cand['distribution_id']}",
                 timeout=45)
        except Exception:
            continue
        _CACHE[key] = cand
        return cand
    return None


def utilization_dataset() -> Optional[dict]:
    """Newest 'Medicare Physician & Other Practitioners - by Provider and Service'."""
    if "cms_util" in _CACHE:
        return _CACHE["cms_util"]                            # type: ignore[return-value]
    try:
        catalog = _get(f"{DATA_CMS}/data.json", timeout=90)
    except Exception:
        return None

    for ds in catalog.get("dataset", []) or []:
        title = ds.get("title", "") or ""
        if "Provider and Service" not in title or "Other Practitioners" not in title:
            continue
        for dist in ds.get("distribution") or []:
            url = dist.get("accessURL") or dist.get("downloadURL") or ""
            m = re.search(r"/dataset/([0-9a-f-]{36})/data", url)
            if m:
                out = {"title": title, "dataset_id": m.group(1),
                       "url": f"{DATA_CMS}/data-api/v1/dataset/{m.group(1)}/data"}
                _CACHE["cms_util"] = out
                return out
    return None


# ----------------------------------------------------------------------------
# Open Payments — industry research payments
# ----------------------------------------------------------------------------
def open_payments(npi: str, max_results: int = 50) -> dict:
    """Research payments made to this NPI."""
    ds = research_dataset()
    if not ds:
        return {"available": False, "reason": "no queryable research dataset",
                "records": [], "count": 0}

    params = {
        "conditions[0][property]": "covered_recipient_npi",
        "conditions[0][operator]": "=",
        "conditions[0][value]": str(npi),
        "limit": max_results,
    }
    try:
        # NOTE: the path is /query/{distribution_id} with NO trailing index.
        # /query/{id}/0 is the resource-index form and 404s for these datasets.
        body = _get(f"{OPENPAYMENTS}/datastore/query/{ds['distribution_id']}",
                    params)
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}",
                "records": [], "count": 0}

    rows = body.get("results", []) or []
    records = []
    total = 0.0
    for row in rows:
        try:
            amount = float(row.get("total_amount_of_payment_usdollars") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        total += amount
        records.append({
            "record_id": row.get("record_id", ""),
            "ref": f"OpenPayments:{row.get('record_id', '')}",
            "amount_usd": amount,
            "payer": row.get(
                "applicable_manufacturer_or_applicable_gpo_making_payment_name", ""),
            "study": (row.get("name_of_study") or "")[:200],
            "program_year": row.get("program_year", ""),
            "date": row.get("date_of_payment", ""),
        })

    payers = sorted({r["payer"] for r in records if r["payer"]})
    studies = sorted({r["study"] for r in records if r["study"]})
    return {
        "available": True,
        "program_year": ds["year"],
        "dataset": ds["title"],
        "count": int(body.get("count", len(records)) or len(records)),
        "returned": len(records),
        "total_usd": round(total, 2),
        "n_payers": len(payers),
        "payers": payers[:15],
        "n_studies": len(studies),
        "studies": studies[:15],
        "records": records,
    }


# ----------------------------------------------------------------------------
# Medicare utilization — practice volume proxy
# ----------------------------------------------------------------------------
def medicare_volume(npi: str, max_rows: int = 200) -> dict:
    """Medicare claim lines for this NPI — a floor on practice volume.

    Medicare-only, so it *underestimates* total practice: a physician with a
    young or commercially-insured population can look far smaller than they are.
    Treat as a floor, never as a total.
    """
    ds = utilization_dataset()
    if not ds:
        return {"available": False, "reason": "utilization dataset not found",
                "records": [], "count": 0}
    try:
        rows = _get(ds["url"], {"filter[Rndrng_NPI]": str(npi), "size": max_rows})
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}",
                "records": [], "count": 0}

    if not isinstance(rows, list) or not rows:
        return {"available": True, "dataset": ds["title"], "count": 0,
                "found": False, "records": []}

    def _num(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    total_services = sum(_num(r.get("Tot_Srvcs")) for r in rows)
    total_benes = sum(_num(r.get("Tot_Benes")) for r in rows)
    first = rows[0]
    records = [{
        "ref": f"HCPCS:{r.get('HCPCS_Cd', '')}",
        "hcpcs": r.get("HCPCS_Cd", ""),
        "description": (r.get("HCPCS_Desc") or "")[:120],
        "n_beneficiaries": _num(r.get("Tot_Benes")),
        "n_services": _num(r.get("Tot_Srvcs")),
    } for r in rows[:25]]
    records.sort(key=lambda r: -r["n_services"])

    return {
        "available": True,
        "dataset": ds["title"],
        "found": True,
        "provider_type": first.get("Rndrng_Prvdr_Type", ""),
        "provider_name": " ".join(filter(None, [
            first.get("Rndrng_Prvdr_First_Name", ""),
            first.get("Rndrng_Prvdr_Last_Org_Name", "")])).strip(),
        "city": first.get("Rndrng_Prvdr_City", ""),
        "state": first.get("Rndrng_Prvdr_State_Abrvtn", ""),
        "n_hcpcs_codes": len(rows),
        "total_services": total_services,
        "total_beneficiaries": total_benes,
        "count": len(rows),
        "records": records,
        "caveat": "Medicare claims only — a floor on volume, not a total.",
    }
