"""
matcher.py — adjudicate one (physician, trial) pair against that trial's rubric.

The matcher has **no tools, deliberately**. Its entire world is two artifacts
already on disk: the rubric from step 4 and the evidence dossier from step 3. It
cannot fetch, so it cannot introduce a fact mid-judgment that nothing else in the
pipeline has seen.

The division of labour is the same one that runs through this project:

  * **The model does the semantic work** — does this evidence meet this
    `satisfies_if`? It returns one verdict per criterion with a rationale, and
    every rationale must cite refs that exist in the dossier.
  * **Python does the arithmetic** — gate logic, weighted score, coverage, and
    the final recommendation. The model is schema-forbidden from emitting a
    score, so it cannot decide the answer and reverse-engineer the reasoning.

Two guards make the output falsifiable rather than merely plausible:

  1. **Ref validation.** Every `evidence_refs` entry is checked against the
     dossier's actual refs. A rationale citing `PMID:99999999` when no such
     record was collected is a fabrication, and it fails validation rather than
     shipping as a citation.
  2. **`unknown` is a real verdict.** When the dossier has nothing on a
     criterion, the honest answer is `unknown`, not a charitable `partial`.
     Coverage falls, and below `MIN_COVERAGE` the recommendation is forced to
     `insufficient_evidence` no matter how well the answered criteria scored.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .rubric import EFFORT, MAX_TOKENS, MODEL, WEIGHTS, cost_usd

# How much a verdict is worth, as a fraction of the criterion's weight.
VERDICT_VALUE = {"satisfied": 1.0, "partial": 0.5, "not_satisfied": 0.0,
                 "unknown": 0.0}

STRONG_T = 0.70          # >= this fraction of max points -> strong_fit
POSSIBLE_T = 0.40        # >= this -> possible_fit
MIN_COVERAGE = 0.50      # below this, we don't claim to know enough to judge

Recommendation = Literal["strong_fit", "possible_fit", "poor_fit",
                         "insufficient_evidence"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Verdict(Strict):
    criterion_id: str
    verdict: Literal["satisfied", "partial", "not_satisfied", "unknown"] = Field(
        description="unknown means the dossier contains nothing that bears on "
                    "this criterion — use it rather than guessing.")
    rationale: str = Field(
        description="One or two sentences tying the specific evidence to this "
                    "criterion's satisfies_if. Name what the evidence showed.")
    evidence_refs: list[str] = Field(
        description="Refs from the dossier that support this verdict, e.g. "
                    "'PMID:41812623', 'NCT02513394', 'NPI:1336121789'. Empty "
                    "for an unknown verdict. Never cite a ref not in the dossier.")


class Adjudication(Strict):
    verdicts: list[Verdict]
    narrative: str = Field(
        description="A short qualitative read of this pairing for a human "
                    "reviewer: the strongest signal, the weakest, and what "
                    "would change the picture. No scores or numbers.")


# ----------------------------------------------------------------------------
# Brief — the matcher's entire world, assembled deterministically
# ----------------------------------------------------------------------------
def collect_refs(dossier: dict) -> set[str]:
    """Every ref the dossier actually contains — the citation whitelist."""
    refs: set[str] = set()
    for entry in dossier.get("entries", []) or []:
        for rec in entry.get("records", []) or []:
            if rec.get("ref"):
                refs.add(str(rec["ref"]))
        for rec in entry.get("mentioned", []) or []:
            if rec.get("ref"):
                refs.add(str(rec["ref"]))
    return refs


def build_brief(rubric_rec: dict, dossier: dict) -> str:
    """Lay the rubric and the evidence side by side, criterion by criterion."""
    rb = rubric_rec["rubric"]
    parts = [
        f"TRIAL {rubric_rec['nct_id']} — {rubric_rec.get('trial_label', '')}",
        f"Target patient: {rb['target_patient']}",
        f"Site burden: {rb['site_burden']}",
        f"(burden source: {rubric_rec['provenance']['burden_source']})",
        "",
        "=" * 70,
        f"PHYSICIAN: {dossier['physician']} (NPI {dossier['npi']})",
        f"{dossier.get('taxonomy', '')} — {dossier.get('city', '')}, "
        f"{dossier.get('state', '')}",
        f"Sources with data: {', '.join(dossier.get('sources_with_data', []))}",
        "",
        "EVIDENCE COLLECTED",
    ]

    for entry in dossier.get("entries", []) or []:
        parts.append(f"\n[{entry['source']}] status={entry['status']} — "
                     f"{entry['summary']}")
        if entry.get("caveat"):
            parts.append(f"  CAVEAT: {entry['caveat']}")
        for rec in (entry.get("records") or [])[:12]:
            bits = [f"ref={rec.get('ref')}"]
            for k in ("title", "taxonomy", "study", "payer", "description",
                      "n_services", "amount_usd", "status", "year"):
                if rec.get(k):
                    bits.append(f"{k}={str(rec[k])[:110]}")
            parts.append("    " + " | ".join(bits))
        extra = entry.get("mentioned") or []
        if extra:
            parts.append(f"    ({len(extra)} weaker text-only mentions, "
                         f"not confirmed roles)")

    if dossier.get("gaps"):
        parts += ["", "KNOWN GAPS IN PUBLIC DATA"]
        for g in dossier["gaps"]:
            parts.append(f"  - {g}")

    parts += ["", "=" * 70, "RUBRIC — adjudicate every criterion below", ""]
    for c in rb["criteria"]:
        parts += [
            f"[{c['id']}] dimension={c['dimension']} "
            f"criticality={c['criticality']}",
            f"  requirement:   {c['requirement']}",
            f"  satisfies_if:  {c['satisfies_if']}",
            f"  data_sources:  {', '.join(c['data_sources'])}",
            "",
        ]
    return "\n".join(parts)


SYSTEM = """You adjudicate whether one physician meets each criterion of a \
rubric built for one specific clinical trial.

You are given the rubric and the physician's evidence dossier. That is your \
entire world — you have no tools and cannot look anything up. If the dossier \
does not contain it, you do not know it.

For every criterion, return exactly one verdict:

  satisfied      the evidence clearly meets satisfies_if
  partial        the evidence points the right way but falls short of the bar
  not_satisfied  the evidence positively indicates the criterion is NOT met
  unknown        the dossier contains nothing bearing on this criterion

The distinction that matters most is **not_satisfied vs unknown**. \
`not_satisfied` is a finding — the evidence shows this physician does not meet \
the bar. `unknown` is an absence — nothing was collected either way. A \
criterion marked `self_report` in its data_sources will almost always be \
`unknown` unless the dossier carries an explicit attestation. Do not upgrade an \
absence into a charitable `partial`; missing evidence is not weak evidence.

Rules for citations:
  * `evidence_refs` must contain refs that appear in the dossier above — \
PMIDs, NCT ids, NPI numbers, OpenPayments record ids, HCPCS codes.
  * Never cite a ref you did not see in the dossier. A rationale resting on an \
invented citation is worse than an honest `unknown`.
  * An `unknown` verdict takes an empty `evidence_refs`.

Weigh evidence honestly:
  * ClinicalTrials.gov "confirmed" roles (listed as an overall official) are \
strong. Text-only "mentions" are weak — do not treat them as roles held.
  * PubMed author search matches surname plus initial, so it can include \
namesakes. Volume alone is weaker than topic-specific relevance.
  * Medicare volume is Medicare claims only — a FLOOR on practice volume, \
never a total. A modest number does not mean a small practice.
  * Open Payments research payments show industry has funded this person's \
research before; the named study can be more informative than the amount.

Do not output scores, points, totals, or a recommendation. Those are computed \
separately from your verdicts. Your job is the judgment on each criterion and a \
short narrative for a human reviewer."""


# ----------------------------------------------------------------------------
# Validation and scoring — Python's half
# ----------------------------------------------------------------------------
def validate(adj: Adjudication, rubric_rec: dict, valid_refs: set[str]) -> list[str]:
    """Problems that make an adjudication untrustworthy. Empty list = valid."""
    problems: list[str] = []
    wanted = {c["id"] for c in rubric_rec["rubric"]["criteria"]}
    got = {v.criterion_id for v in adj.verdicts}

    for missing in sorted(wanted - got):
        problems.append(f"no verdict for criterion '{missing}'")
    for extra in sorted(got - wanted):
        problems.append(f"verdict for unknown criterion '{extra}'")
    if len(adj.verdicts) != len(got):
        problems.append("duplicate verdicts for the same criterion")

    for v in adj.verdicts:
        phantom = [r for r in v.evidence_refs if r not in valid_refs]
        if phantom:
            problems.append(
                f"{v.criterion_id}: cites ref(s) not in the dossier: "
                f"{', '.join(phantom[:3])}")
        if v.verdict == "unknown" and v.evidence_refs:
            problems.append(f"{v.criterion_id}: verdict is unknown but cites refs")
        if v.verdict in ("satisfied", "partial") and not v.evidence_refs:
            problems.append(
                f"{v.criterion_id}: verdict '{v.verdict}' with no evidence_refs")
    return problems


def score(rubric_rec: dict, verdicts: list[dict]) -> dict:
    """Gate, weighted score, coverage, recommendation — all computed here."""
    criteria = {c["id"]: c for c in rubric_rec["rubric"]["criteria"]}
    by_id = {v["criterion_id"]: v for v in verdicts}

    earned = possible = 0.0
    gate_failures: list[str] = []
    for cid, crit in criteria.items():
        v = by_id.get(cid, {"verdict": "unknown"})
        if crit["criticality"] == "hard_gate":
            if v["verdict"] in ("not_satisfied", "partial"):
                gate_failures.append(cid)
            continue
        w = WEIGHTS[crit["criticality"]]
        possible += w
        earned += w * VERDICT_VALUE[v["verdict"]]

    answered = [v for v in verdicts if v["verdict"] != "unknown"]
    coverage = len(answered) / len(criteria) if criteria else 0.0
    pct = (earned / possible) if possible else 0.0

    # Order matters: a failed gate outranks a good score, and thin coverage
    # outranks a confident-looking one.
    if gate_failures:
        rec: Recommendation = "poor_fit"
        reason = f"hard gate not met: {', '.join(gate_failures)}"
    elif coverage < MIN_COVERAGE:
        rec = "insufficient_evidence"
        reason = (f"only {coverage:.0%} of criteria could be adjudicated "
                  f"(minimum {MIN_COVERAGE:.0%})")
    elif pct >= STRONG_T:
        rec, reason = "strong_fit", f"scored {pct:.0%} of available points"
    elif pct >= POSSIBLE_T:
        rec, reason = "possible_fit", f"scored {pct:.0%} of available points"
    else:
        rec, reason = "poor_fit", f"scored {pct:.0%} of available points"

    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1

    dims: dict[str, dict] = {}
    for cid, crit in criteria.items():
        d = crit["dimension"]
        slot = dims.setdefault(d, {"earned": 0.0, "possible": 0.0, "n": 0})
        slot["n"] += 1
        if crit["criticality"] == "hard_gate":
            continue
        w = WEIGHTS[crit["criticality"]]
        slot["possible"] += w
        slot["earned"] += w * VERDICT_VALUE[by_id.get(cid, {"verdict": "unknown"})["verdict"]]
    for d in dims.values():
        d["pct"] = round(d["earned"] / d["possible"], 3) if d["possible"] else None

    return {
        "score": round(earned, 2),
        "max_score": round(possible, 2),
        "score_pct": round(pct, 3),
        "coverage": round(coverage, 3),
        "n_criteria": len(criteria),
        "n_answered": len(answered),
        "verdict_counts": counts,
        "gate_failures": gate_failures,
        "recommendation": rec,
        "reason": reason,
        "by_dimension": dims,
        "thresholds": {"strong": STRONG_T, "possible": POSSIBLE_T,
                       "min_coverage": MIN_COVERAGE},
    }


# ----------------------------------------------------------------------------
# The agent
# ----------------------------------------------------------------------------
def match(rubric_rec: dict, dossier: dict, max_attempts: int = 3,
          model: str = "", verbose: bool = True) -> dict:
    """Adjudicate one pair. Retries with validation errors fed back."""
    import anthropic

    model = model or MODEL
    client = anthropic.Anthropic()
    valid_refs = collect_refs(dossier)
    brief = build_brief(rubric_rec, dossier)
    messages: list[dict] = [{"role": "user", "content":
                             f"{brief}\n\nAdjudicate every criterion."}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    attempts: list[dict] = []

    for attempt in range(1, max_attempts + 1):
        response = client.messages.parse(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            messages=messages, output_format=Adjudication,
        )
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens

        if response.stop_reason == "refusal":
            raise RuntimeError(f"refused: {response.stop_details}")

        adj = response.parsed_output
        problems = (["response did not parse"] if adj is None
                    else validate(adj, rubric_rec, valid_refs))
        attempts.append({"attempt": attempt, "problems": problems})

        if not problems:
            return assemble(rubric_rec, dossier, adj.model_dump(), usage,
                            attempts, model)

        if verbose:
            print(f"    attempt {attempt}: {len(problems)} problem(s) — retrying")
            for p in problems[:3]:
                print(f"      - {p}")
        messages += [
            {"role": "assistant", "content": json.dumps(
                adj.model_dump() if adj else {})},
            {"role": "user", "content":
             "That adjudication has problems:\n"
             + "\n".join(f"  - {p}" for p in problems)
             + "\n\nFix only these and resubmit every verdict."},
        ]

    raise RuntimeError(f"adjudication failed after {max_attempts} attempts: "
                       f"{attempts[-1]['problems']}")


def assemble(rubric_rec: dict, dossier: dict, adj: dict, usage: dict,
             attempts: list, model: str = MODEL, variant: str = "public_only") -> dict:
    """Join verdicts to the rubric and compute the numbers."""
    criteria = {c["id"]: c for c in rubric_rec["rubric"]["criteria"]}
    detailed = []
    for v in adj["verdicts"]:
        c = criteria.get(v["criterion_id"], {})
        detailed.append({**v,
                         "dimension": c.get("dimension"),
                         "criticality": c.get("criticality"),
                         "requirement": c.get("requirement"),
                         "satisfies_if": c.get("satisfies_if"),
                         "data_sources": c.get("data_sources", [])})
    return {
        "nct_id": rubric_rec["nct_id"],
        "trial_label": rubric_rec.get("trial_label", ""),
        "npi": dossier["npi"],
        "physician": dossier["physician"],
        "specialty": dossier.get("specialty", ""),
        "variant": variant,
        "model": model,
        "scoring": score(rubric_rec, adj["verdicts"]),
        "verdicts": detailed,
        "narrative": adj.get("narrative", ""),
        "trace": {"attempts": attempts, "usage": usage,
                  "cost_usd": round(cost_usd(usage, model), 4)},
    }


def write(report: dict, data_dir: Path) -> Path:
    out_dir = data_dir / "match_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if report["variant"] == "public_only" else f"__{report['variant']}"
    path = out_dir / f"{report['nct_id']}__{report['npi']}{suffix}.json"
    path.write_text(json.dumps(report, indent=2))
    return path
