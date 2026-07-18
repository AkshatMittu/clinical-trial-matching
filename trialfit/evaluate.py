"""
evaluate.py — does the scorer actually measure fit, or just produce numbers?

Running without crashing is not the same as working. A scorer that returns
`strong_fit — 13.5/14` for everyone looks exactly like one that works, right up
until someone asks whether it would ever say no.

Three layers, cheapest and most informative first.

**1. Tier separation (free).** Step 2 assigned every pair an expected-fit tier
from public signals — `known` / `strong` / `moderate` / `weak` — and nothing
downstream reads it. It is a held-out label. If real scores rank the tiers in
order, the scorer is tracking something real; if the tiers overlap completely,
it is not measuring fit and every individual score is suspect. Only `known` is
ground truth (the registry says so); the rest are heuristics, so partial
separation is the expected result and full separation would be surprising.

**2. Perturbation (a few model calls).** Delete an evidence source from a
dossier and re-adjudicate. If the score does not move, the matcher is not
reading the evidence — it is scoring the physician's name and specialty. This
catches a failure that no amount of eyeballing the output would.

**3. Integrity (free).** Recompute each stored score from its stored verdicts
and check they agree; confirm every criterion got exactly one verdict and every
cited ref exists. Guards against drift once scoring rules change.

No LLM judge. It is the most expensive layer and the least informative, and the
prior version of this project found that chasing a judge's nitpicks actively
degraded the agents.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Optional

from . import matcher

TIER_ORDER = ["known", "strong", "moderate", "weak"]


# ----------------------------------------------------------------------------
# 1. Tier separation
# ----------------------------------------------------------------------------
def _auc(pos: list[float], neg: list[float]) -> Optional[float]:
    """P(random positive scores above random negative). 0.5 = coin flip."""
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 3)


def tier_separation(data_dir: Path, variant: str = "public_only") -> dict:
    """Compare real scores against the tiers assigned before any scoring."""
    pairs = json.loads((data_dir / "scoring_pairs.json").read_text())
    reports_dir = data_dir / "match_reports"

    rows = []
    for p in pairs:
        suffix = "" if variant == "public_only" else f"__{variant}"
        path = reports_dir / f"{p['nct_id']}__{p['npi']}{suffix}.json"
        if not path.exists():
            continue
        rep = json.loads(path.read_text())
        rows.append({
            "nct_id": p["nct_id"], "npi": p["npi"],
            "physician": p["physician"], "tier": p["tier"],
            "score_pct": rep["scoring"]["score_pct"],
            "coverage": rep["scoring"]["coverage"],
            "recommendation": rep["scoring"]["recommendation"],
        })

    if not rows:
        return {"available": False,
                "reason": "no match reports yet — run scripts/run_match.py"}

    by_tier: dict[str, dict] = {}
    for tier in TIER_ORDER:
        vals = [r["score_pct"] for r in rows if r["tier"] == tier]
        if not vals:
            continue
        by_tier[tier] = {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 3),
            "median": round(statistics.median(vals), 3),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
        }

    present = [t for t in TIER_ORDER if t in by_tier]
    means = [by_tier[t]["mean"] for t in present]
    monotonic = all(a >= b for a, b in zip(means, means[1:]))

    strong_side = [r["score_pct"] for r in rows if r["tier"] in ("known", "strong")]
    weak_side = [r["score_pct"] for r in rows if r["tier"] in ("moderate", "weak")]
    auc = _auc(strong_side, weak_side)
    spread = round(max(means) - min(means), 3) if means else 0.0

    if auc is None:
        verdict = "not enough pairs scored on both sides to compare"
    elif auc >= 0.80 and spread >= 0.15:
        verdict = ("Scores separate the tiers clearly. The scorer is tracking "
                   "something real.")
    elif auc >= 0.65:
        verdict = ("Scores separate the tiers weakly. Directionally right, but "
                   "individual scores should not be leaned on hard.")
    else:
        verdict = ("Scores do NOT separate the tiers. The scorer is not "
                   "measuring fit — likely causes: rubric criteria too generic, "
                   "the matcher being charitable to everyone, or evidence that "
                   "does not discriminate.")

    return {
        "available": True, "variant": variant, "n_pairs": len(rows),
        "by_tier": by_tier, "monotonic": monotonic,
        "mean_spread": spread,
        "auc_strong_vs_weak": auc,
        "verdict": verdict,
        "rows": sorted(rows, key=lambda r: -r["score_pct"]),
        "caveat": ("Only the `known` tier is ground truth — the registry lists "
                   "that physician as an official on that trial. `strong` / "
                   "`moderate` / `weak` are heuristics over trial metadata, so "
                   "partial separation is expected and perfect separation would "
                   "suggest the tiers and the scorer share an assumption."),
    }


# ----------------------------------------------------------------------------
# 2. Perturbation
# ----------------------------------------------------------------------------
def perturb(rubric_rec: dict, dossier: dict, baseline: dict,
            model: str = "", verbose: bool = True) -> dict:
    """Remove one evidence source at a time and see whether the score moves."""
    import copy
    results = []
    base_pct = baseline["scoring"]["score_pct"]
    sources = [e["source"] for e in dossier.get("entries", [])
               if e.get("status") == "found" and e["source"] != "NPPES"]

    for source in sources:
        stripped = copy.deepcopy(dossier)
        stripped["entries"] = [e for e in stripped["entries"]
                               if e["source"] != source]
        try:
            rep = matcher.match(rubric_rec, stripped, model=model, verbose=False)
        except Exception as ex:
            results.append({"removed": source, "error": str(ex)})
            continue
        delta = round(rep["scoring"]["score_pct"] - base_pct, 3)
        results.append({
            "removed": source,
            "score_pct": rep["scoring"]["score_pct"],
            "delta": delta,
            "moved": abs(delta) >= 0.01,
            "cost_usd": rep["trace"]["cost_usd"],
        })
        if verbose:
            print(f"    without {source:<22} {rep['scoring']['score_pct']:.0%} "
                  f"({delta:+.0%})")

    moved = [r for r in results if r.get("moved")]
    inert = [r for r in results if r.get("moved") is False]
    return {
        "baseline_score_pct": base_pct,
        "n_sources_tested": len(results),
        "n_moved": len(moved),
        "inert_sources": [r["removed"] for r in inert],
        "results": results,
        "total_cost_usd": round(sum(r.get("cost_usd", 0) for r in results), 4),
        "verdict": ("The matcher reads the evidence — removing sources moves the "
                    "score." if len(moved) >= max(1, len(results) // 2) else
                    "Removing evidence barely moved the score. The matcher may "
                    "be scoring the physician's identity rather than their "
                    "record."),
    }


# ----------------------------------------------------------------------------
# 3. Integrity
# ----------------------------------------------------------------------------
def integrity(data_dir: Path) -> dict:
    """Every stored report must be internally consistent and honestly cited."""
    checks, failures = [], []
    reports = sorted((data_dir / "match_reports").glob("*.json")) \
        if (data_dir / "match_reports").exists() else []

    for path in reports:
        rep = json.loads(path.read_text())
        name = path.stem
        rubric_path = data_dir / "rubrics" / f"{rep['nct_id']}.json"
        if not rubric_path.exists():
            failures.append(f"{name}: rubric missing")
            continue
        rubric_rec = json.loads(rubric_path.read_text())

        recomputed = matcher.score(rubric_rec, rep["verdicts"])
        if abs(recomputed["score"] - rep["scoring"]["score"]) > 1e-6:
            failures.append(f"{name}: stored score {rep['scoring']['score']} != "
                            f"recomputed {recomputed['score']}")
        if recomputed["recommendation"] != rep["scoring"]["recommendation"]:
            failures.append(f"{name}: stored recommendation "
                            f"{rep['scoring']['recommendation']} != recomputed "
                            f"{recomputed['recommendation']}")

        ids = [v["criterion_id"] for v in rep["verdicts"]]
        wanted = {c["id"] for c in rubric_rec["rubric"]["criteria"]}
        if set(ids) != wanted:
            failures.append(f"{name}: verdict ids do not match the rubric")
        if len(ids) != len(set(ids)):
            failures.append(f"{name}: duplicate verdicts")

        dossier_path = data_dir / "evidence" / f"{rep['npi']}.json"
        if dossier_path.exists():
            dossier = json.loads(dossier_path.read_text())
            filtered, _ = matcher.exclude_target_trial(
                dossier, rep["nct_id"], rubric_rec.get("trial_label", ""))
            valid = matcher.collect_refs(filtered) | {
                f"attestation:{c}" for c in wanted}
            for v in rep["verdicts"]:
                bad = [r for r in v.get("evidence_refs", []) if r not in valid]
                if bad:
                    failures.append(f"{name}/{v['criterion_id']}: phantom refs "
                                    f"{bad[:2]}")
        checks.append(name)

    return {"n_reports": len(checks), "n_failures": len(failures),
            "failures": failures,
            "verdict": "All stored reports are internally consistent."
            if not failures else f"{len(failures)} integrity failure(s)."}


def write(result: dict, data_dir: Path, name: str = "evaluation") -> Path:
    out_dir = data_dir / "evaluation"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(result, indent=2))
    return path
