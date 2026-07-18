"""
pipeline.py — run one pair end to end, and record the run so it can be replayed.

    rubric -> adjudicate -> precedent -> interview -> re-adjudicate -> report

The **trajectory** is what makes this demoable. A live run makes 3-4 model calls
and several API round trips; that is a minute or two of staring at a terminal,
which is most of a three-minute demo spent waiting. So every run writes a
trajectory: the finished artifacts plus the per-step timings the live run
actually took.

`replay()` then reads that trajectory and re-emits the same steps from disk with
**zero API calls**, optionally re-pacing them so the audience sees the pipeline
progress rather than a finished page appearing instantly. What's replayed is the
real run's output, not a mock — if the demo shows a score, that score came from
a model call that genuinely happened.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import interview as iv
from . import matcher, patient, precedent, report
from .rubric import cost_usd


@dataclass
class Step:
    """One stage of the run, as the UI sees it."""
    key: str
    label: str
    status: str = "pending"          # pending | running | done | skipped | error
    detail: str = ""
    seconds: float = 0.0
    cost_usd: float = 0.0


STEPS = [
    ("rubric", "Build the trial's rubric"),
    ("adjudicate", "Score the physician against it"),
    ("precedent", "Look up what comparable trials did"),
    ("patient", "Score participant experience in those trials"),
    ("interview", "Close the gaps public data left"),
    ("report", "Render the report"),
]


def _load(path: Path) -> Optional[dict]:
    return json.loads(path.read_text()) if path.exists() else None


def run(nct_id: str, npi: str, data_dir: Path,
        answers: Optional[dict[str, str]] = None,
        model: str = "", skip_interview: bool = False,
        refresh_precedent: bool = False,
        emit: Optional[Callable[[Step], None]] = None) -> dict:
    """Execute the pipeline for one pair, writing every artifact as it goes."""
    steps: list[Step] = [Step(k, l) for k, l in STEPS]
    by_key = {s.key: s for s in steps}

    def mark(key: str, status: str, detail: str = "", t0: float = 0.0,
             cost: float = 0.0) -> None:
        s = by_key[key]
        s.status, s.detail, s.cost_usd = status, detail, cost
        if t0:
            s.seconds = round(time.monotonic() - t0, 2)
        if emit:
            emit(s)

    started = time.monotonic()

    # --- rubric (built in step 4; this stage loads and reports it) ---------
    t0 = time.monotonic()
    rubric_rec = _load(data_dir / "rubrics" / f"{nct_id}.json")
    if rubric_rec is None:
        mark("rubric", "error", "no rubric — run scripts/build_rubrics.py", t0)
        raise FileNotFoundError(f"no rubric for {nct_id}")
    mark("rubric", "done",
         f"{rubric_rec['scoring']['n_criteria']} criteria, "
         f"max {rubric_rec['scoring']['max_score']} pts", t0)

    dossier = _load(data_dir / "evidence" / f"{npi}.json")
    if dossier is None:
        raise FileNotFoundError(f"no evidence dossier for NPI {npi}")

    # --- adjudicate --------------------------------------------------------
    t0 = time.monotonic()
    mark("adjudicate", "running")
    match = matcher.match(rubric_rec, dossier, model=model, verbose=False)
    matcher.write(match, data_dir)
    sc = match["scoring"]
    mark("adjudicate", "done",
         f"{sc['recommendation']} — {sc['score']}/{sc['max_score']} "
         f"({sc['coverage']:.0%} coverage)", t0, match["trace"]["cost_usd"])

    # --- precedent (no model call) ----------------------------------------
    t0 = time.monotonic()
    mark("precedent", "running")
    prec = None if refresh_precedent else precedent.load(nct_id, data_dir)
    if prec is None:
        info = _load(data_dir / "trial_info" / f"{nct_id}.json")
        prec = precedent.find_similar(info) if info else {"available": False,
                                                          "reason": "no trial_info"}
        precedent.write(prec, nct_id, data_dir)
    mark("precedent", "done", precedent.headline(prec), t0)

    # --- patient-experience proxy (no model call) -------------------------
    t0 = time.monotonic()
    mark("patient", "running")
    cohort = None if refresh_precedent else patient.load(nct_id, data_dir)
    if cohort is None:
        info = _load(data_dir / "trial_info" / f"{nct_id}.json") or {}
        # Retention is only comparable within a setting, so match on it.
        setting = ""
        manifest = _load(data_dir / "trials_manifest.json")
        if isinstance(manifest, list):
            setting = next((r.get("setting", "") for r in manifest
                            if r.get("nct_id") == nct_id), "")
        cohort = patient.build_cohort(prec, data_dir, setting=setting,
                                      verbose=False)
        patient.write(cohort, nct_id, data_dir)
    mark("patient", "done",
         (f"median proxy {cohort['median_proxy']}/100 across "
          f"{cohort['n_scored']} trials with posted results")
         if cohort.get("available") else cohort.get("reason", ""), t0)

    # --- interview ---------------------------------------------------------
    interview_rec = None
    if skip_interview:
        mark("interview", "skipped", "skipped by request")
    else:
        t0 = time.monotonic()
        mark("interview", "running")
        interview_rec = iv.conduct(rubric_rec, dossier, match, answers=answers,
                                   model=model, verbose=False)
        if interview_rec.get("rescored"):
            matcher.write(interview_rec["rescored"], data_dir)
            d = iv.delta(interview_rec["before"], interview_rec["after"])
            detail = (f"{len(interview_rec['gaps'])} gaps → "
                      f"{d['recommendation'][0]} → {d['recommendation'][1]}")
        else:
            detail = (f"{len(interview_rec['gaps'])} gaps, "
                      f"{len(interview_rec['questions'])} questions, no answers "
                      f"supplied")
        mark("interview", "done", detail, t0, interview_rec["trace"]["cost_usd"])

    # --- report ------------------------------------------------------------
    t0 = time.monotonic()
    mark("report", "running")
    html = report.render(match, rubric_rec, dossier, precedent=prec,
                         patient_cohort=cohort, interview=interview_rec)
    path = report.write(html, nct_id, npi, data_dir)
    mark("report", "done", str(path), t0)

    final = (interview_rec["rescored"] if interview_rec
             and interview_rec.get("rescored") else match)
    trajectory = {
        "nct_id": nct_id,
        "npi": npi,
        "physician": final["physician"],
        "trial_label": final["trial_label"],
        "recommendation": final["scoring"]["recommendation"],
        "score": final["scoring"]["score"],
        "max_score": final["scoring"]["max_score"],
        "coverage": final["scoring"]["coverage"],
        "report_path": str(path.relative_to(data_dir.parent)),
        "total_seconds": round(time.monotonic() - started, 2),
        "total_cost_usd": round(sum(s.cost_usd for s in steps), 4),
        "steps": [vars(s) for s in steps],
        "artifacts": {
            "rubric": f"rubrics/{nct_id}.json",
            "dossier": f"evidence/{npi}.json",
            "match": f"match_reports/{nct_id}__{npi}.json",
            "match_interviewed": (f"match_reports/{nct_id}__{npi}__interviewed.json"
                                  if interview_rec and interview_rec.get("rescored")
                                  else None),
            "precedent": f"precedent/{nct_id}.json",
            "patient_proxy": f"patient_proxy/{nct_id}.json",
        },
        "interview": ({"gaps": interview_rec["gaps"],
                       "questions": interview_rec["questions"],
                       "answers": interview_rec["answers"],
                       "attestations": interview_rec["attestations"],
                       "before": interview_rec["before"],
                       "after": interview_rec["after"]}
                      if interview_rec else None),
    }
    write_trajectory(trajectory, data_dir)
    return trajectory


# ----------------------------------------------------------------------------
# Trajectory storage and replay
# ----------------------------------------------------------------------------
def write_trajectory(traj: dict, data_dir: Path) -> Path:
    out_dir = data_dir / "trajectories"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{traj['nct_id']}__{traj['npi']}.json"
    path.write_text(json.dumps(traj, indent=2))
    return path


def load_trajectory(nct_id: str, npi: str, data_dir: Path) -> Optional[dict]:
    return _load(data_dir / "trajectories" / f"{nct_id}__{npi}.json")


def list_trajectories(data_dir: Path) -> list[dict]:
    out_dir = data_dir / "trajectories"
    if not out_dir.exists():
        return []
    out = []
    for p in sorted(out_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return out


def replay(nct_id: str, npi: str, data_dir: Path, pace: float = 0.0,
           emit: Optional[Callable[[Step], None]] = None) -> dict:
    """Re-emit a recorded run from disk. Zero API calls.

    `pace` scales the original wall-clock timings: 0 is instant, 1.0 replays at
    the speed the live run actually took, 0.15 gives a demo the *feel* of the
    pipeline working without spending the real minute and a half on it.
    """
    traj = load_trajectory(nct_id, npi, data_dir)
    if traj is None:
        raise FileNotFoundError(
            f"no trajectory for {nct_id}__{npi} — run the pipeline live once "
            f"first (scripts/run_match.py --prebuild)")
    for raw in traj["steps"]:
        step = Step(**raw)
        if emit and step.status not in ("skipped",):
            running = Step(step.key, step.label, "running")
            emit(running)
            if pace:
                time.sleep(min(step.seconds * pace, 2.5))
        if emit:
            emit(step)
    return traj
