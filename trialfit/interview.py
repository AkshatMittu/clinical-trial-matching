"""
interview.py — close the gap public data structurally cannot.

Public physician sources answer *"is this person a researcher"* well and
*"does this person treat THIS patient population"* badly. Medicare volume is the
only population signal available and it counts Medicare claims only. So the
high-weight `patient_population` criteria come back `unknown`, coverage drops
below `MIN_COVERAGE`, and a genuinely good match lands at
`insufficient_evidence`.

That verdict is correct. The pipeline should not guess its way past it — it
should **ask the physician**, and then say plainly that the answer came from
them rather than from a public record.

The loop:

    find gaps -> one targeted question per gap -> collect answers
              -> merge as clearly-labelled self-report -> re-adjudicate

Self-report is never laundered into looking like public evidence. Every attested
record carries `source="Physician self-report (interview)"`, a
`ref="attestation:<criterion_id>"`, and a `[SELF-REPORTED]` prefix on its
summary, so the matcher weighs it as testimony and the final report shows the
reader exactly which criteria rest on it.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from . import matcher
from .rubric import EFFORT, MAX_TOKENS, MODEL, cost_usd

ATTESTATION_SOURCE = "Physician self-report (interview)"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Question(Strict):
    criterion_id: str
    question: str = Field(
        description="One direct question to the physician, answerable in a "
                    "sentence or two. Ask for the specific fact satisfies_if "
                    "needs — not an open-ended prompt.")
    why: str = Field(
        description="One line the physician can read: why we're asking and "
                    "what public data couldn't tell us.")


class QuestionSet(Strict):
    questions: list[Question]


# ----------------------------------------------------------------------------
# Gaps
# ----------------------------------------------------------------------------
def find_gaps(rubric_rec: dict, report: dict) -> list[dict]:
    """Criteria a physician could settle that public data didn't.

    A `not_satisfied` verdict is NOT a gap — the evidence positively showed the
    criterion isn't met, and re-asking would just invite the subject to
    overturn a finding. Only absence is a gap.
    """
    criteria = {c["id"]: c for c in rubric_rec["rubric"]["criteria"]}
    gaps = []
    for v in report["verdicts"]:
        if v["verdict"] not in ("unknown", "partial"):
            continue
        c = criteria.get(v["criterion_id"])
        if not c:
            continue
        gaps.append({
            "criterion_id": c["id"],
            "dimension": c["dimension"],
            "criticality": c["criticality"],
            "requirement": c["requirement"],
            "satisfies_if": c["satisfies_if"],
            "data_sources": c["data_sources"],
            "current_verdict": v["verdict"],
            "self_report_expected": "self_report" in c["data_sources"],
        })
    # Ask about the criteria that move the score most, first.
    order = {"hard_gate": 0, "primary": 1, "secondary": 2}
    gaps.sort(key=lambda g: (order.get(g["criticality"], 3),
                             g["current_verdict"] != "unknown"))
    return gaps


QUESTION_SYSTEM = """You write short interview questions for a physician being \
considered as a site investigator on a clinical trial.

For each gap you are given, write ONE question that would let the physician \
settle it. The question must target the specific fact in `satisfies_if` — if the \
criterion needs an annual patient count, ask for the number; if it needs \
infrastructure, ask whether they have it.

Rules:
  * Answerable in one or two sentences. No compound questions.
  * Ask for facts the physician actually knows about their own practice — \
patient volumes, staffing, equipment, prior trial roles. Do not ask them to \
speculate about the trial or to evaluate themselves ("would you be a good fit?").
  * Neutral phrasing. Do not signal the answer you're hoping for; a question \
that telegraphs the desired answer produces a useless attestation.
  * `why` is shown to the physician — one plain line about what public records \
couldn't confirm."""


def generate_questions(rubric_rec: dict, gaps: list[dict], model: str = "",
                       max_questions: int = 6) -> tuple[list[dict], dict]:
    """Ask the model for one question per gap. Returns (questions, usage)."""
    import anthropic

    model = model or MODEL
    gaps = gaps[:max_questions]
    if not gaps:
        return [], {"input_tokens": 0, "output_tokens": 0}

    brief = [f"TRIAL: {rubric_rec['nct_id']} — {rubric_rec.get('trial_label')}",
             f"Target patient: {rubric_rec['rubric']['target_patient']}",
             "", "GAPS TO CLOSE:"]
    for g in gaps:
        brief += [
            f"\n[{g['criterion_id']}] ({g['criticality']}, {g['dimension']})",
            f"  requirement:  {g['requirement']}",
            f"  satisfies_if: {g['satisfies_if']}",
            f"  status:       {g['current_verdict']} from public data",
        ]

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=model, max_tokens=MAX_TOKENS, system=QUESTION_SYSTEM,
        thinking={"type": "adaptive"}, output_config={"effort": EFFORT},
        messages=[{"role": "user", "content": "\n".join(brief)
                   + "\n\nWrite one question per gap."}],
        output_format=QuestionSet,
    )
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    qs = response.parsed_output.questions if response.parsed_output else []
    wanted = {g["criterion_id"] for g in gaps}
    return [q.model_dump() for q in qs if q.criterion_id in wanted], usage


def template_questions(gaps: list[dict]) -> list[dict]:
    """Keyless fallback — shows the gaps without spending a call."""
    return [{
        "criterion_id": g["criterion_id"],
        "question": f"Regarding \"{g['requirement']}\" — can you confirm: "
                    f"{g['satisfies_if']}?",
        "why": "Public records could not confirm this.",
    } for g in gaps]


# ----------------------------------------------------------------------------
# Merging answers back in
# ----------------------------------------------------------------------------
def apply_answers(dossier: dict, gaps: list[dict],
                  answers: dict[str, str]) -> tuple[dict, list[dict]]:
    """Return (augmented dossier, attestation records).

    The augmented dossier is a deep copy — the public-only dossier stays intact
    on disk so the before/after comparison remains reproducible.
    """
    augmented = copy.deepcopy(dossier)
    by_id = {g["criterion_id"]: g for g in gaps}
    records = []

    unmatched = []
    for cid, answer in answers.items():
        if not answer or not answer.strip() or answer.strip().lower() == "skip":
            continue
        if cid not in by_id:
            # A scripted answer for a criterion that isn't actually a gap —
            # usually a stale demo_answers.json after the rubric changed.
            # Attesting to it would inject evidence for a criterion nothing
            # asked about, so drop it and say so rather than failing silently.
            unmatched.append(cid)
            continue
        g = by_id[cid]
        records.append({
            "ref": f"attestation:{cid}",
            "criterion_id": cid,
            "source": ATTESTATION_SOURCE,
            "summary": f"[SELF-REPORTED] {answer.strip()}",
            "requirement": g.get("requirement", ""),
            "satisfies_if": g.get("satisfies_if", ""),
        })

    if records:
        augmented.setdefault("entries", []).append({
            "source": ATTESTATION_SOURCE,
            "status": "found",
            "summary": f"{len(records)} criteria attested by the physician "
                       f"during a gap-closing interview",
            "n_records": len(records),
            "records": records,
            "caveat": "Physician attestation, not a public record. Weigh "
                      "accordingly — it is testimony about their own practice.",
        })
        augmented["sources_queried"] = list(augmented.get("sources_queried", [])) \
            + [ATTESTATION_SOURCE]
        augmented["sources_with_data"] = list(augmented.get("sources_with_data", [])) \
            + [ATTESTATION_SOURCE]
        augmented["interview_applied"] = True
    if unmatched:
        augmented["unmatched_answers"] = unmatched
    return augmented, records


def conduct(rubric_rec: dict, dossier: dict, report: dict,
            answers: Optional[dict[str, str]] = None,
            ask: Optional[Callable[[dict], str]] = None,
            model: str = "", use_llm: bool = True,
            verbose: bool = True) -> dict:
    """Full loop: gaps -> questions -> answers -> re-adjudicate.

    `answers` supplies scripted responses (demo/replay); `ask` is a callable for
    live collection. With neither, the interview stops after the questions so a
    caller can render them and come back.
    """
    gaps = find_gaps(rubric_rec, report)
    usage = {"input_tokens": 0, "output_tokens": 0}
    if not gaps:
        return {"gaps": [], "questions": [], "answers": {}, "attestations": [],
                "before": report["scoring"], "after": report["scoring"],
                "rescored": None, "trace": {"usage": usage, "cost_usd": 0.0}}

    if use_llm:
        questions, qusage = generate_questions(rubric_rec, gaps, model=model)
        usage["input_tokens"] += qusage["input_tokens"]
        usage["output_tokens"] += qusage["output_tokens"]
    else:
        questions = template_questions(gaps)

    if verbose:
        print(f"    {len(gaps)} gap(s), {len(questions)} question(s)")

    collected = dict(answers or {})
    if ask is not None:
        for q in questions:
            if q["criterion_id"] in collected:
                continue
            collected[q["criterion_id"]] = ask(q)

    if not collected:
        return {"gaps": gaps, "questions": questions, "answers": {},
                "attestations": [], "before": report["scoring"],
                "after": report["scoring"], "rescored": None,
                "trace": {"usage": usage,
                          "cost_usd": round(cost_usd(usage, model or MODEL), 4)}}

    augmented, attestations = apply_answers(dossier, gaps, collected)
    rescored = matcher.match(rubric_rec, augmented, model=model, verbose=verbose)
    rescored["variant"] = "interviewed"
    usage["input_tokens"] += rescored["trace"]["usage"]["input_tokens"]
    usage["output_tokens"] += rescored["trace"]["usage"]["output_tokens"]

    return {
        "gaps": gaps,
        "questions": questions,
        "answers": collected,
        "attestations": attestations,
        "before": report["scoring"],
        "after": rescored["scoring"],
        "rescored": rescored,
        "augmented_dossier": augmented,
        "trace": {"usage": usage,
                  "cost_usd": round(cost_usd(usage, model or MODEL), 4)},
    }


def delta(before: dict, after: dict) -> dict:
    """The before/after summary a reader actually wants."""
    return {
        "score": (before["score"], after["score"]),
        "score_pct": (before["score_pct"], after["score_pct"]),
        "coverage": (before["coverage"], after["coverage"]),
        "recommendation": (before["recommendation"], after["recommendation"]),
        "changed": before["recommendation"] != after["recommendation"],
    }
