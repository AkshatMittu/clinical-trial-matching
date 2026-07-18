"""
ctgov.py — ClinicalTrials.gov API v2 client.

The only place that talks to CT.gov. Everything above this module works with
plain dicts (the v2 study record) and never builds a URL itself.

Public data, no API key required.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://clinicaltrials.gov/api/v2/studies"
CDN = "https://cdn.clinicaltrials.gov/large-docs"

PAGE_SIZE = 100
POLITE_DELAY = 0.12          # seconds between calls — CT.gov asks for restraint


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=0.8,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"accept": "application/json"})
    return s


SESSION = _session()


def _get(url: str, params: dict) -> dict:
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    time.sleep(POLITE_DELAY)
    return r.json()


def dig(obj: Any, *keys, default=None):
    """Walk nested dicts safely: dig(rec, 'protocolSection', 'statusModule')."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


# ----------------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------------
def _search_params(condition: str, statuses: Iterable[str],
                   term: Optional[str] = None) -> dict:
    params = {
        "query.cond": condition,
        "filter.overallStatus": ",".join(statuses),
        "pageSize": PAGE_SIZE,
        "countTotal": "true",
    }
    if term:
        params["query.term"] = term
    return params


def count(condition: str, statuses: Iterable[str],
          term: Optional[str] = None) -> int:
    """How many studies match — for the selection funnel, without pulling them.

    The v2 /stats/size endpoint ignores query filters, so ask /studies for a
    one-row page and read totalCount.
    """
    params = _search_params(condition, statuses, term)
    params["pageSize"] = 1
    data = _get(API, params)
    return int(data.get("totalCount", 0)) if isinstance(data, dict) else 0


def iter_studies(condition: str, statuses: Iterable[str],
                 term: Optional[str] = None, limit: int = 400) -> Iterator[dict]:
    """Yield full study records, paging until `limit` or the results run out."""
    params = _search_params(condition, statuses, term)
    pulled = 0
    while True:
        data = _get(API, params)
        for study in data.get("studies", []):
            yield study
            pulled += 1
            if pulled >= limit:
                return
        token = data.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token


def fetch_one(nct_id: str) -> Optional[dict]:
    """One full record by NCT id, or None if CT.gov doesn't have it."""
    try:
        return _get(f"{API}/{nct_id}", {})
    except requests.HTTPError:
        return None


# ----------------------------------------------------------------------------
# Local cache — raw records are the source of truth for every later stage
# ----------------------------------------------------------------------------
def save_record(record: dict, raw_dir: Path) -> Optional[Path]:
    nct = dig(record, "protocolSection", "identificationModule", "nctId")
    if not nct:
        return None
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{nct}.json"
    path.write_text(json.dumps(record, indent=2))
    return path


def load_record(nct_id: str, raw_dir: Path) -> Optional[dict]:
    """Cached record if present, else fetch from CT.gov and cache it."""
    path = raw_dir / f"{nct_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    record = fetch_one(nct_id)
    if record is not None:
        save_record(record, raw_dir)
    return record


# ----------------------------------------------------------------------------
# Protocol documents
# ----------------------------------------------------------------------------
def protocol_url(record: dict) -> Optional[str]:
    """Direct download URL for the posted protocol PDF, if this trial has one.

    Protocol availability skews heavily toward completed trials — recruiting
    trials usually have nothing posted yet.
    """
    nct = dig(record, "protocolSection", "identificationModule", "nctId")
    docs = dig(record, "documentSection", "largeDocumentModule", "largeDocs",
               default=[])
    for doc in docs:
        if doc.get("hasProtocol") and doc.get("filename"):
            return f"{CDN}/{nct[-2:]}/{nct}/{doc['filename']}"
    return None
