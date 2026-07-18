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

import xml.etree.ElementTree as ET

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


# How many publications get an abstract, and how much of each we keep.
# Titles are cheap; abstracts are not. Ten recent papers at ~700 chars is about
# 1.7k tokens into the matcher's brief — enough to settle "has this person
# worked on PIK3CA" without drowning the evidence that decides everything else.
ABSTRACT_LIMIT = 10
ABSTRACT_CHARS = 700


def _get_xml(path: str, params: dict) -> Optional[ET.Element]:
    """efetch returns XML, not JSON — esummary has no abstract field."""
    params = dict(params)
    if API_KEY:
        params["api_key"] = API_KEY
    try:
        r = SESSION.get(f"{EUTILS}/{path}", params=params, timeout=45)
        r.raise_for_status()
        time.sleep(POLITE_DELAY)
        return ET.fromstring(r.content)
    except Exception:
        return None


def fetch_abstracts(pmids: list[str]) -> dict[str, str]:
    """PMID -> abstract text. Missing or unparseable ones are simply absent.

    Structured abstracts split into labelled sections (BACKGROUND, METHODS,
    RESULTS); those labels carry real meaning for judging a paper's relevance,
    so they are kept rather than flattened away.
    """
    if not pmids:
        return {}
    root = _get_xml("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids),
                                    "retmode": "xml", "rettype": "abstract"})
    if root is None:
        return {}
    out: dict[str, str] = {}
    for art in root.iter("PubmedArticle"):
        pid_el = art.find(".//PMID")
        if pid_el is None or not pid_el.text:
            continue
        parts = []
        for ab in art.iter("AbstractText"):
            text = "".join(ab.itertext()).strip()
            if not text:
                continue
            label = ab.get("Label") or ab.get("NlmCategory")
            parts.append(f"{label}: {text}" if label else text)
        if parts:
            joined = " ".join(parts)
            out[pid_el.text] = (joined[:ABSTRACT_CHARS] + "…"
                                if len(joined) > ABSTRACT_CHARS else joined)
    return out


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
           recent_years: int = 0, max_results: int = 25,
           with_abstracts: bool = True) -> dict:
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

    if with_abstracts and records:
        # Only the most recent handful — results come back date-sorted, and an
        # abstract for every one of 25 papers would crowd out the rest of the
        # dossier without adding much.
        abstracts = fetch_abstracts([r["pmid"] for r in records[:ABSTRACT_LIMIT]])
        for r in records:
            if r["pmid"] in abstracts:
                r["abstract"] = abstracts[r["pmid"]]

    return {"available": True, "query": term, "count": total,
            "returned": len(records),
            "n_abstracts": sum(1 for r in records if r.get("abstract")),
            "records": records}


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
