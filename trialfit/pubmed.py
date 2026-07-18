"""
pubmed.py — publication evidence via NCBI E-utilities.

Answers "has this physician published in the trial's disease area, and recently?"
Public; an NCBI_API_KEY only raises the rate limit (3/s -> 10/s).

Author search is inherently fuzzy. PubMed's `[Author]` field matches on
"Lastname Initials", so "Mayer EL" also catches every other E. Mayer. We record
the query we ran alongside the results so the ambiguity stays visible downstream
rather than being laundered into a clean-looking count.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
API_KEY = os.environ.get("NCBI_API_KEY", "")
POLITE_DELAY = 0.15 if API_KEY else 0.35      # NCBI: 10/s with a key, 3/s without


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=0.8,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


SESSION = _session()


def _get(path: str, params: dict) -> dict:
    params = dict(params)
    params.setdefault("retmode", "json")
    if API_KEY:
        params["api_key"] = API_KEY
    r = SESSION.get(f"{EUTILS}/{path}", params=params, timeout=30)
    r.raise_for_status()
    time.sleep(POLITE_DELAY)
    return r.json()


def author_query(first_name: str, last_name: str) -> str:
    """PubMed author term: 'Erica' + 'Mayer' -> 'Mayer E[Author]'."""
    initial = (first_name or "").strip()[:1].upper()
    return f"{last_name} {initial}[Author]" if initial else f"{last_name}[Author]"


def search(first_name: str, last_name: str, topic: str = "",
           recent_years: int = 0, max_results: int = 25) -> dict:
    """Publications for an author, optionally narrowed by topic and recency."""
    term = author_query(first_name, last_name)
    if topic:
        term += f" AND ({topic})"
    params = {"db": "pubmed", "term": term, "retmax": max_results,
              "sort": "date"}
    if recent_years:
        params["reldate"] = recent_years * 365
        params["datetype"] = "pdat"

    try:
        res = _get("esearch.fcgi", params)["esearchresult"]
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}",
                "query": term, "count": 0, "records": []}

    pmids = res.get("idlist", []) or []
    total = int(res.get("count", 0) or 0)
    records = summarize(pmids) if pmids else []
    return {"available": True, "query": term, "count": total,
            "returned": len(records), "records": records}


def summarize(pmids: list[str]) -> list[dict]:
    """Title / journal / year / authors for a list of PMIDs."""
    if not pmids:
        return []
    try:
        data = _get("esummary.fcgi", {"db": "pubmed", "id": ",".join(pmids)})
    except Exception:
        return [{"pmid": p, "ref": f"PMID:{p}"} for p in pmids]

    out = []
    result = data.get("result", {}) or {}
    for pmid in result.get("uids", []) or []:
        item = result.get(pmid, {}) or {}
        authors = [a.get("name", "") for a in (item.get("authors") or [])]
        out.append({
            "pmid": pmid,
            "ref": f"PMID:{pmid}",
            "title": (item.get("title") or "")[:300],
            "journal": item.get("source", "") or "",
            "pubdate": item.get("pubdate", "") or "",
            "year": (item.get("pubdate", "") or "")[:4],
            "n_authors": len(authors),
            "first_author": authors[0] if authors else "",
            "last_author": authors[-1] if authors else "",
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return out
