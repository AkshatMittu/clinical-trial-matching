"""
rubric.py — the evaluator agent: one trial in, one tailored rubric out.

Every trial needs different things from a site investigator. A first-in-human
dose-escalation study needs a site that can handle intensive PK sampling and
dose-limiting-toxicity review; a 400-site adjuvant phase 3 needs throughput and
a large eligible population. Scoring both against one fixed checklist would
measure the checklist, not the fit. So the rubric is generated per trial.

Three constraints shape this module, each of them load-bearing:

  1. **The agent has no tools.** Step 3 already assembled the trial record; the
     agent receives a brief built deterministically by `build_brief()` and
     nothing else. It cannot fetch, and therefore cannot invent, a fact
     mid-generation — every criterion has to trace to something in the brief.

  2. **Python owns every number.** The model assigns each criterion a
     *criticality label* (`hard_gate` / `primary` / `secondary`); `WEIGHTS`
     turns labels into points. The model never emits a weight or a total, so it
     cannot tune the arithmetic to reach a score it likes.

  3. **Provenance comes from the record, not the model.** Whether burden was
     read from a real Schedule of Assessments or inferred from phase and design
     is set from `trial_info`, regardless of what the rubric says about itself.

The rubric is the *work order* for the physician side: each criterion declares
which of our five public sources could verify it, so the next stage knows what
to go looking for — and which criteria public data can never settle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Sonnet 5 is the default: near-Opus quality on this kind of judgment work at
# 2.5x lower cost. Note that Opus 4.7 and 4.8 are priced identically ($5/$25) —
# stepping 4.8 -> 4.7 saves nothing; the saving comes from the Sonnet tier.
MODEL = os.environ.get("TRIALFIT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 16000
EFFORT = os.environ.get("TRIALFIT_EFFORT", "high")

# USD per million tokens (input, output). Used only to report spend as it
# accrues — a running budget is worth more than a pre-run estimate.
PRICING = {
    "claude-sonnet-5": (2.00, 10.00),      # intro pricing through 2026-08-31
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),      # same price as 4.8
    "claude-haiku-4-5": (1.00, 5.00),
}


def cost_usd(usage: dict, model: str = MODEL) -> float:
    """Dollar cost of one call's token usage. 0.0 for an unpriced model."""
    rates = PRICING.get(model)
    if not rates:
        return 0.0
    in_rate, out_rate = rates
    return (usage.get("input_tokens", 0) / 1e6 * in_rate
            + usage.get("output_tokens", 0) / 1e6 * out_rate)

# Criticality -> points. A hard gate isn't weighted; failing it caps the whole
# match regardless of how the other criteria score.
WEIGHTS = {"primary": 3, "secondary": 1}

DIMENSIONS = ("expertise", "patient_population", "operational_capacity",
              "trial_execution")

# The five sources step 3 actually collects. A criterion that cites anything
# else is asking for evidence we cannot produce.
DATA_SOURCES = ("nppes", "pubmed", "clinicaltrials_gov", "open_payments",
                "medicare", "self_report")

MIN_CRITERIA, MAX_CRITERIA = 6, 12


# ----------------------------------------------------------------------------
# Schema — what the agent is allowed to return
# ----------------------------------------------------------------------------
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Criterion(Strict):
    id: str = Field(description="short snake_case id, e.g. 'breast_onc_specialty'")
    dimension: Literal["expertise", "patient_population",
                       "operational_capacity", "trial_execution"]
    criticality: Literal["hard_gate", "primary", "secondary"] = Field(
        description="hard_gate: disqualifying if unmet. primary: strongly "
                    "load-bearing. secondary: helpful but not decisive.")
    requirement: str = Field(
        description="What this trial needs from the investigator, in one "
                    "sentence, specific to THIS trial.")
    satisfies_if: str = Field(
        description="The concrete, checkable condition that would satisfy "
                    "this. Must be verifiable against a record, not a vibe.")
    data_sources: list[Literal["nppes", "pubmed", "clinicaltrials_gov",
                               "open_payments", "medicare", "self_report"]] = Field(
        description="Which sources could verify this. Use 'self_report' when "
                    "no public source can settle it.")
    derived_from: str = Field(
        description="What in the trial record produced this criterion "
                    "(e.g. 'eligibility: HR+/HER2- Stage II-III').")


class Rubric(Strict):
    target_patient: str = Field(
        description="One paragraph: the patient this trial enrolls, as a "
                    "practice would recognize them.")
    site_burden: str = Field(
        description="What running this trial actually demands of a site — "
                    "visit cadence, procedures, infrastructure.")
    criteria: list[Criterion]
    excluded_screening: list[str] = Field(
        description="Per-patient screening criteria you deliberately did NOT "
                    "turn into requirements, and why they say nothing about "
                    "physician fit.")


# ----------------------------------------------------------------------------
# The brief — deterministic input, built by code
# ----------------------------------------------------------------------------
def _cap(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f"\n[...truncated, {len(text)} chars total]"


def build_brief(info: dict) -> str:
    """Assemble the trial brief the agent reasons over.

    Only population-defining criteria are surfaced as requirement material;
    screening criteria are listed separately and explicitly marked out of
    scope, so the split made in step 3 is visible rather than assumed.
    """
    elig = info.get("eligibility", {}) or {}
    proto = info.get("protocol", {}) or {}
    soa = proto.get("schedule_of_assessments", {}) or {}

    parts = [
        f"TRIAL {info['nct_id']} — {info.get('acronym') or info.get('brief_title', '')}",
        f"Official title: {info.get('official_title', '')}",
        f"Status: {info.get('status')} | Phase: {'/'.join(info.get('phases') or [])} "
        f"| Enrollment: {info.get('enrollment')} | Sites: {info.get('n_sites')} "
        f"({info.get('n_us_sites')} US)",
        f"Sponsor: {info.get('lead_sponsor')} ({info.get('sponsor_class')})",
        f"Conditions: {', '.join(info.get('conditions') or [])}",
        "",
        "BRIEF SUMMARY",
        _cap(info.get("brief_summary", ""), 2000),
        "",
        "INTERVENTIONS",
    ]
    for iv in (info.get("interventions") or [])[:12]:
        parts.append(f"  [{iv.get('type')}] {iv.get('name')} — "
                     f"{_cap(iv.get('description', ''), 200)}")

    parts += ["", "PRIMARY OUTCOMES"]
    for o in (info.get("primary_outcomes") or [])[:6]:
        parts.append(f"  {o.get('measure')} (timeframe: {o.get('timeframe')})")

    parts += ["", "POPULATION-DEFINING ELIGIBILITY "
              "(these describe the patients a practice must already treat — "
              "requirement material)"]
    for c in (elig.get("population_defining") or [])[:30]:
        parts.append(f"  [{c.get('kind')}] {c.get('text')}")

    parts += ["", "PER-PATIENT SCREENING CRITERIA "
              "(checked at enrolment for each patient — NOT physician-fit "
              "material; list these under excluded_screening)"]
    for c in (elig.get("screening") or [])[:20]:
        parts.append(f"  [{c.get('kind')}] {c.get('text')}")

    parts += ["", f"Demographics: {info.get('sex')}, min age {info.get('min_age')}"]

    parts += ["", "OPERATIONAL BURDEN"]
    if soa.get("found"):
        conf = soa.get("confidence", "unknown")
        parts.append(f"Source: posted protocol, Schedule of Assessments "
                     f"(page {soa.get('page_number')}, confidence={conf}).")
        if conf == "heading_only":
            parts.append("NOTE: the schedule heading matched but the page is not "
                         "structured as a visit grid — treat this text as weak "
                         "evidence of burden and say so in site_burden.")
        parts.append(_cap(soa.get("text", ""), 6000))
    else:
        parts.append("No Schedule of Assessments available. Burden must be "
                     "INFERRED from phase, design, interventions and outcomes — "
                     "say so explicitly in site_burden rather than implying it "
                     "was read from a protocol.")

    sites = info.get("sites_sample") or []
    if sites:
        parts += ["", f"SITE FOOTPRINT (sample of {info.get('n_sites')})"]
        for s in sites[:8]:
            parts.append(f"  {s.get('facility')} — {s.get('city')}, "
                         f"{s.get('state')} {s.get('country')}")
    return "\n".join(parts)


SYSTEM = """You build scoring rubrics that judge whether a physician would be a \
good SITE INVESTIGATOR for one specific clinical trial.

You are given a trial record. Produce a rubric tailored to THAT trial. A rubric \
that would apply equally well to any oncology trial has failed — every criterion \
must trace to something specific in the record you were given, named in \
`derived_from`.

The judgment that matters most is what belongs in a rubric at all:

  * A criterion belongs if it describes what the PRACTICE or the PHYSICIAN must \
already be, have, or do — the patients they treat, their specialty, their trial \
experience, their site's capacity for this protocol's demands.
  * A criterion does NOT belong if it is checked per enrolled patient — lab \
thresholds, ECOG status, washout windows, consent, pregnancy tests. These say \
nothing about whether this physician is a good site. List them in \
`excluded_screening` with a one-line reason.

Across the four dimensions:
  expertise            — specialty, disease-area depth, publication record
  patient_population   — does their practice contain these patients, in volume
  operational_capacity — can the site carry this protocol's visit and procedure load
  trial_execution      — prior investigator roles, sponsor relationships, GCP track record

Rules:
  * Produce %d-%d criteria, with at least one in each dimension.
  * Exactly one or two criteria may be `hard_gate` — reserve it for things that \
genuinely disqualify (e.g. wrong specialty entirely). Everything else is \
`primary` or `secondary`.
  * `satisfies_if` must be checkable against a record. "Has relevant experience" \
is not checkable. "Listed as an investigator on >=1 completed breast cancer \
trial" is.
  * `data_sources` must name only sources that could actually settle the \
criterion. The available sources are:
      nppes              — identity, specialty taxonomy, practice location
      pubmed             — publication record
      clinicaltrials_gov — prior investigator roles on other trials
      open_payments      — industry research payments (proxy for trial experience)
      medicare           — procedure/beneficiary volume (Medicare only: a FLOOR \
on practice volume, never a total)
      self_report        — nothing public can settle it; the physician must attest
  * Be honest about `self_report`. Public data is good at "is this person a \
researcher" and bad at "does this person treat THIS patient population." If a \
criterion genuinely cannot be settled publicly, mark it `self_report` rather \
than pointing at a source that can only weakly gesture at it.
  * Do not invent facts. If the brief says burden was not available from a \
protocol, say the burden is inferred — do not describe a visit schedule you \
were not given.""" % (MIN_CRITERIA, MAX_CRITERIA)


# ----------------------------------------------------------------------------
# Validation — Python's checks, run after the schema's
# ----------------------------------------------------------------------------
def validate(rubric: Rubric) -> list[str]:
    """Structural problems the JSON schema can't express. Empty list = valid."""
    problems: list[str] = []
    n = len(rubric.criteria)
    if not MIN_CRITERIA <= n <= MAX_CRITERIA:
        problems.append(f"produced {n} criteria; need {MIN_CRITERIA}-{MAX_CRITERIA}")

    seen = [c.dimension for c in rubric.criteria]
    missing = [d for d in DIMENSIONS if d not in seen]
    if missing:
        problems.append(f"no criteria for dimension(s): {', '.join(missing)}")

    gates = [c for c in rubric.criteria if c.criticality == "hard_gate"]
    if not gates:
        problems.append("no hard_gate criterion; exactly one or two are required")
    elif len(gates) > 2:
        problems.append(f"{len(gates)} hard gates; at most 2 allowed "
                        f"({', '.join(c.id for c in gates)})")

    ids = [c.id for c in rubric.criteria]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        problems.append(f"duplicate criterion ids: {', '.join(sorted(dupes))}")

    for c in rubric.criteria:
        if not c.data_sources:
            problems.append(f"{c.id}: no data_sources listed")
        if len(c.satisfies_if.split()) < 4:
            problems.append(f"{c.id}: satisfies_if is too vague to check")
    return problems


def score_model(rubric: Rubric) -> dict:
    """The arithmetic — computed here, never by the model.

    A hard gate carries no weight because it isn't scored on a scale: failing
    it caps the match outright, which a large negative weight would only
    approximate.
    """
    weighted = [c for c in rubric.criteria if c.criticality != "hard_gate"]
    max_score = sum(WEIGHTS[c.criticality] for c in weighted)
    by_dimension: dict[str, dict] = {}
    for d in DIMENSIONS:
        rows = [c for c in rubric.criteria if c.dimension == d]
        by_dimension[d] = {
            "n_criteria": len(rows),
            "max_points": sum(WEIGHTS[c.criticality] for c in rows
                              if c.criticality != "hard_gate"),
        }
    publicly_verifiable = [c for c in rubric.criteria
                           if any(s != "self_report" for s in c.data_sources)]
    return {
        "weights": WEIGHTS,
        "max_score": max_score,
        "n_criteria": len(rubric.criteria),
        "hard_gates": [c.id for c in rubric.criteria
                       if c.criticality == "hard_gate"],
        "by_dimension": by_dimension,
        "n_publicly_verifiable": len(publicly_verifiable),
        "n_self_report_only": len(rubric.criteria) - len(publicly_verifiable),
        "public_coverage": round(len(publicly_verifiable) / len(rubric.criteria), 2)
        if rubric.criteria else 0.0,
    }


# ----------------------------------------------------------------------------
# The agent
# ----------------------------------------------------------------------------
def build(info: dict, max_attempts: int = 3, verbose: bool = True,
          model: str = "") -> dict:
    """Generate one trial's rubric. Retries with the validation errors fed back."""
    import anthropic

    model = model or MODEL
    client = anthropic.Anthropic()
    brief = build_brief(info)
    messages: list[dict] = [{"role": "user", "content":
                             f"{brief}\n\nBuild the rubric for this trial."}]

    usage_total = {"input_tokens": 0, "output_tokens": 0}
    attempts: list[dict] = []

    for attempt in range(1, max_attempts + 1):
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            messages=messages,
            output_format=Rubric,
        )
        usage_total["input_tokens"] += response.usage.input_tokens
        usage_total["output_tokens"] += response.usage.output_tokens

        if response.stop_reason == "refusal":
            raise RuntimeError(f"refused: {response.stop_details}")

        rubric = response.parsed_output
        if rubric is None:
            problems = ["response did not parse into the rubric schema"]
        else:
            problems = validate(rubric)

        attempts.append({"attempt": attempt, "problems": problems,
                         "stop_reason": response.stop_reason})
        if verbose and problems:
            print(f"    attempt {attempt}: {len(problems)} problem(s) — retrying")
            for p in problems[:4]:
                print(f"      - {p}")

        if not problems:
            return _assemble(info, rubric, usage_total, attempts, model)

        # Hand the failures back and let it fix them, rather than discarding a
        # mostly-good rubric over one bad criterion.
        messages += [
            {"role": "assistant", "content": json.dumps(
                rubric.model_dump() if rubric else {})},
            {"role": "user", "content":
             "That rubric has problems:\n"
             + "\n".join(f"  - {p}" for p in problems)
             + "\n\nFix only these and resubmit the complete rubric."},
        ]

    raise RuntimeError(f"{info['nct_id']}: rubric failed validation after "
                       f"{max_attempts} attempts: {attempts[-1]['problems']}")


def _assemble(info: dict, rubric: Rubric, usage: dict, attempts: list,
              model: str = MODEL) -> dict:
    """Wrap the validated rubric with provenance the model doesn't get to assert."""
    proto = info.get("protocol", {}) or {}
    soa = proto.get("schedule_of_assessments", {}) or {}
    return {
        "nct_id": info["nct_id"],
        "trial_label": info.get("acronym") or info.get("brief_title", "")[:60],
        "model": model,
        "effort": EFFORT,
        "rubric": rubric.model_dump(),
        "scoring": score_model(rubric),
        # Set from the trial record, not from anything the model said.
        "provenance": {
            "burden_source": "protocol" if soa.get("found") else "inferred",
            "soa_confidence": soa.get("confidence") if soa.get("found") else None,
            "soa_page": soa.get("page_number") if soa.get("found") else None,
            "protocol_available": bool(proto.get("available")),
            "n_population_criteria": len(
                (info.get("eligibility", {}) or {}).get("population_defining", [])),
            "n_screening_criteria": len(
                (info.get("eligibility", {}) or {}).get("screening", [])),
        },
        "trace": {"attempts": attempts, "usage": usage,
                  "cost_usd": round(cost_usd(usage, model), 4)},
    }


def write(record: dict, data_dir: Path) -> Path:
    out_dir = data_dir / "rubrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{record['nct_id']}.json"
    path.write_text(json.dumps(record, indent=2))
    return path
