"""
protocol.py — protocol PDF download and text extraction.

The posted protocol is the only honest source of **operational burden**: how many
visits, what happens at each, which procedures a site must be able to perform.
That lives in the Schedule of Assessments, a visit-by-visit grid usually buried
50+ pages in.

Without a protocol, burden can only be inferred from phase and enrollment — a
guess. Anything derived that way is flagged `inferred`, never presented as read
from source.

Extraction is cached per trial: the PDF is fetched once and page text is stored
as JSON, so re-runs never re-download or re-parse.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import ctgov

# Section headings for the visit grid, in the order we prefer to match them.
SOA_PATTERNS = [
    r"schedule of assessment", r"schedule of activit", r"schedule of event",
    r"schedule of procedure", r"study flow chart", r"time and events",
    r"visit schedule", r"study calendar",
]

# A trailing dotted leader or bare page number marks a contents line, not the
# section itself — otherwise every hit lands on the table of contents.
_LEADER_RE = re.compile(r"(\.{2,}\s*)?\d{1,4}\s*$")


def download(nct_id: str, url: str, pdf_dir: Path) -> Optional[Path]:
    """Fetch the protocol PDF, unless it's already on disk."""
    pdf_dir.mkdir(parents=True, exist_ok=True)
    path = pdf_dir / f"{nct_id}_protocol.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        r = ctgov.SESSION.get(url, timeout=180)
        r.raise_for_status()
    except Exception:
        return None
    if not r.content.startswith(b"%PDF"):
        return None
    path.write_bytes(r.content)
    return path


def extract_pages(pdf_path: Path) -> list[str]:
    """Per-page text. Requires pdfplumber; returns [] if unavailable."""
    try:
        import pdfplumber
    except ImportError:
        return []
    pages: list[str] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
    except Exception:
        return []
    return pages


def cached_pages(nct_id: str, pdf_path: Path, cache_dir: Path) -> list[str]:
    """Page text, extracted once and cached as JSON."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{nct_id}_pages.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except json.JSONDecodeError:
            pass
    pages = extract_pages(pdf_path)
    if pages:
        cache.write_text(json.dumps(pages))
    return pages


def _is_contents_page(text: str) -> bool:
    """A page that's mostly dotted-leader lines is the table of contents."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 6:
        return False
    leaders = sum(1 for l in lines if _LEADER_RE.search(l.strip()))
    return leaders / len(lines) > 0.45


def find_section(pages: list[str], patterns: list[str]) -> Optional[int]:
    """Page index of the first real occurrence of a section, skipping contents."""
    for i, text in enumerate(pages):
        low = (text or "").lower()
        if not any(re.search(p, low) for p in patterns):
            continue
        if _is_contents_page(text):
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if any(re.search(p, stripped.lower()) for p in patterns):
                if _LEADER_RE.search(stripped):     # a contents entry, keep going
                    continue
                return i
    return None


# Column headers of a visit grid. Prose about the schedule rarely stacks these.
_VISIT_RE = re.compile(
    r"screening|baseline|cycle \d|day \d|week \d|end of treatment|"
    r"follow-?up|\beot\b|\bq\d+w\b|randomi[sz]ation",
    re.I)
# A standalone X (the "do this at this visit" mark), not the letter inside a word.
_XMARK_RE = re.compile(r"(?<![A-Za-z])[XxX✓](?![A-Za-z])")


def grid_score(text: str) -> dict:
    """How much does this page look like a visit grid rather than prose?

    A real Schedule of Assessments is a table: procedure rows crossed with visit
    columns, cells marked X. Body text that merely *references* the schedule has
    the words but none of the structure — which is how a cross-reference on page
    49 of the PALLAS protocol beat the actual grid on page 58.
    """
    if not text:
        return {"score": 0, "x_marks": 0, "visit_terms": 0}
    x_marks = len(_XMARK_RE.findall(text))
    visit_terms = len(set(m.lower() for m in _VISIT_RE.findall(text)))
    return {"score": x_marks + 4 * visit_terms, "x_marks": x_marks,
            "visit_terms": visit_terms}


def schedule_of_assessments(pages: list[str], window: int = 3,
                            min_x_marks: int = 10,
                            min_visit_terms: int = 3) -> dict:
    """Locate the visit grid and return its text.

    Ranks every page by grid structure and takes the best one that clears the
    thresholds. Falls back to the heading match only when no page looks like a
    table, and says so — a heading without a grid is a weaker result and
    downstream should know that.
    """
    scored = []
    for i, text in enumerate(pages):
        g = grid_score(text)
        if g["x_marks"] >= min_x_marks and g["visit_terms"] >= min_visit_terms:
            scored.append((g["score"], i, g))

    if scored:
        scored.sort(key=lambda s: (-s[0], s[1]))
        _, idx, g = scored[0]
        confidence, reason = "grid", "page structured as a visit grid"
    else:
        idx = find_section(pages, SOA_PATTERNS)
        if idx is None:
            return {"found": False,
                    "reason": "no visit grid and no schedule heading"}
        g = grid_score(pages[idx])
        confidence, reason = "heading_only", (
            "matched a schedule heading but the page is not structured as a "
            "grid — may be a cross-reference rather than the schedule itself")

    chunk = "\n".join(pages[idx:idx + window])
    return {
        "found": True,
        "confidence": confidence,
        "reason": reason,
        "page_index": idx,
        "page_number": idx + 1,
        "x_marks": g["x_marks"],
        "visit_terms": g["visit_terms"],
        "n_pages_captured": min(window, len(pages) - idx),
        "text": chunk[:12000],
        "truncated": len(chunk) > 12000,
        "total_chars": len(chunk),
    }


def load(nct_id: str, protocol_url: str, pdf_dir: Path, cache_dir: Path) -> dict:
    """Download, extract and locate the burden section for one trial."""
    if not protocol_url:
        return {"available": False, "reason": "no protocol posted",
                "burden_source": "inferred"}
    path = download(nct_id, protocol_url, pdf_dir)
    if path is None:
        return {"available": False, "reason": "download failed",
                "burden_source": "inferred"}
    pages = cached_pages(nct_id, path, cache_dir)
    if not pages:
        return {"available": False, "reason": "text extraction failed",
                "burden_source": "inferred", "pdf_path": str(path)}
    return {
        "available": True,
        "burden_source": "protocol",
        "pdf_path": str(path),
        "size_mb": round(path.stat().st_size / 1e6, 2),
        "n_pages": len(pages),
        "total_chars": sum(len(p) for p in pages),
        "schedule_of_assessments": schedule_of_assessments(pages),
    }
