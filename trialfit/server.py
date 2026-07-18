"""
server.py — the demo UI. Standard library only, no framework, no build step.

`python scripts/serve_demo.py` and open a browser. Nothing to install: the whole
thing is `http.server` plus one HTML file, because a demo that needs `npm
install` on the morning of the demo is a demo that fails.

The screen mirrors the pipeline. Physicians on the left; pick one and every
trial in their bucket is listed. Pick a trial and the stages stream in as they
resolve — trial information, rubric, the evidence the matcher actually looked
at, the score, comparable trials, the patient-experience proxy, then the
interview and the re-score. Each stage opens to show its own working, because
"trust the score" is not a demo; "here is what the score is made of" is.

Two modes:

  * **replay** (default) — reads a recorded trajectory from disk. No API key, no
    network, instant. What replays is a real run's output, not a mock.
  * **live** — runs the pipeline for real. Same screen, same stages, just slower
    and billed.

Progress streams over SSE, which is one `EventSource` in the browser and a
generator on this side. No websockets, no polling.
"""
from __future__ import annotations

import json
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import interview as iv
from . import matcher, patient, pipeline, precedent, report, rubric

# Interviews that have been asked but not yet answered, keyed "{nct}__{npi}".
# A live interview is a two-phase exchange — questions out, answers back — so
# the questions and the dossier they were generated against have to survive
# between the two requests.
PENDING: dict[str, dict] = {}

DATA: Path = Path("data")
UI_PATH = Path(__file__).resolve().parent / "ui.html"


def _load(p: Path) -> Optional[dict]:
    return json.loads(p.read_text()) if p.exists() else None


# ----------------------------------------------------------------------------
# Payload builders — each stage's "show your working" detail
# ----------------------------------------------------------------------------
def physicians_payload() -> list[dict]:
    roster = _load(DATA / "physicians_roster.json") or []
    pairs = json.loads((DATA / "scoring_pairs.json").read_text())
    trajectories = {f"{t['nct_id']}__{t['npi']}"
                    for t in pipeline.list_trajectories(DATA)}
    out = []
    for p in roster:
        if not p.get("demo_role"):
            continue
        mine = [q for q in pairs if q["npi"] == p["npi"]]
        out.append({
            "npi": p["npi"], "name": p["display_name"],
            "specialty": p.get("specialty", "").replace("_", " "),
            "taxonomy": p.get("taxonomy", ""),
            "city": p.get("city", ""), "state": p.get("state", ""),
            "n_trials": len(mine),
            "trials": [{
                "nct_id": q["nct_id"], "label": q["label"], "tier": q["tier"],
                "recorded": f"{q['nct_id']}__{q['npi']}" in trajectories,
            } for q in mine],
        })
    return out


def trial_stage(nct_id: str) -> dict:
    info = _load(DATA / "trial_info" / f"{nct_id}.json") or {}
    el = info.get("eligibility", {}) or {}
    proto = info.get("protocol", {}) or {}
    soa = proto.get("schedule_of_assessments", {}) or {}
    return {
        "nct_id": nct_id,
        "title": info.get("official_title") or info.get("brief_title", ""),
        "acronym": info.get("acronym", ""),
        "phase": "/".join(info.get("phases") or []),
        "status": info.get("status", ""),
        "enrollment": info.get("enrollment"),
        "n_sites": info.get("n_sites"), "n_us_sites": info.get("n_us_sites"),
        "sponsor": info.get("lead_sponsor", ""),
        "conditions": info.get("conditions", []),
        "interventions": [f"[{i['type']}] {i['name']}"
                          for i in (info.get("interventions") or [])[:8]],
        "n_population_defining": len(el.get("population_defining") or []),
        "n_screening": len(el.get("screening") or []),
        "population_sample": [c["text"][:150]
                              for c in (el.get("population_defining") or [])[:5]],
        "screening_sample": [c["text"][:150]
                             for c in (el.get("screening") or [])[:4]],
        "protocol_available": bool(proto.get("available")),
        "soa_found": bool(soa.get("found")),
        "soa_page": soa.get("page_number"),
        "soa_confidence": soa.get("confidence"),
        "burden_source": "protocol" if soa.get("found") else "inferred",
    }


def rubric_stage(nct_id: str) -> dict:
    rec = _load(DATA / "rubrics" / f"{nct_id}.json")
    if not rec:
        return {"available": False,
                "reason": "no rubric — run scripts/build_rubrics.py"}
    rb = rec["rubric"]
    return {
        "available": True,
        "model": rec.get("model"), "effort": rec.get("effort"),
        "target_patient": rb["target_patient"],
        "site_burden": rb["site_burden"],
        "criteria": rb["criteria"],
        "excluded_screening": rb.get("excluded_screening", []),
        "scoring": rec["scoring"],
        "provenance": rec.get("provenance", {}),
        "cost_usd": rec.get("trace", {}).get("cost_usd", 0),
    }


def _acronym(nct_id: str) -> str:
    """The trial's acronym, from the rubric if built, else the manifest.

    The publication filter needs it to spot results papers, and it must work
    before any rubric exists — otherwise the UI shows 0 papers excluded and
    quietly misrepresents what the matcher can see.
    """
    rec = _load(DATA / "rubrics" / f"{nct_id}.json")
    if rec and rec.get("trial_label"):
        return rec["trial_label"]
    info = _load(DATA / "trial_info" / f"{nct_id}.json")
    if info and info.get("acronym"):
        return info["acronym"]
    manifest = _load(DATA / "trials_manifest.json")
    if isinstance(manifest, list):
        for row in manifest:
            if row.get("nct_id") == nct_id:
                return row.get("acronym", "")
    return ""


def evidence_stage(nct_id: str, npi: str) -> dict:
    """What the matcher is actually allowed to look at — after exclusion."""
    dossier = _load(DATA / "evidence" / f"{npi}.json") or {}
    filtered, removed = matcher.exclude_target_trial(
        dossier, nct_id, _acronym(nct_id))
    return {
        "physician": dossier.get("physician", ""),
        "sources": [{
            "source": e["source"], "status": e["status"],
            "summary": e["summary"], "caveat": e.get("caveat", ""),
            "n_records": len(e.get("records") or []),
            "records": [{k: r.get(k) for k in
                         ("ref", "title", "taxonomy", "study", "payer",
                          "description", "n_services", "amount_usd", "year")
                         if r.get(k)}
                        for r in (e.get("records") or [])[:8]],
        } for e in filtered.get("entries", [])],
        "gaps": filtered.get("gaps", []),
        "excluded": {
            "target_trial": nct_id,
            "roles": removed["roles"],
            "publications": removed["publications"],
            "why": ("Evidence about the trial being scored is removed before "
                    "adjudication. Otherwise 'is this person a plausible "
                    "investigator for this trial?' is answered by 'they are an "
                    "investigator on this trial.' In production this is a no-op "
                    "— a new trial has no history to exclude."),
        },
        "n_refs_available": len(matcher.collect_refs(filtered)),
    }


def precedent_stage(nct_id: str) -> dict:
    p = precedent.load(nct_id, DATA)
    return p or {"available": False, "reason": "not collected"}


def patient_stage(nct_id: str) -> dict:
    p = patient.load(nct_id, DATA)
    return p or {"available": False, "reason": "not collected"}


def match_stage(nct_id: str, npi: str, variant: str = "") -> dict:
    suffix = f"__{variant}" if variant else ""
    rep = _load(DATA / "match_reports" / f"{nct_id}__{npi}{suffix}.json")
    if not rep:
        return {"available": False, "reason": "not scored yet"}
    return {"available": True, "scoring": rep["scoring"],
            "verdicts": rep["verdicts"], "narrative": rep.get("narrative", ""),
            "model": rep.get("model"),
            "cost_usd": rep.get("trace", {}).get("cost_usd", 0)}


def interview_stage(nct_id: str, npi: str) -> dict:
    traj = pipeline.load_trajectory(nct_id, npi, DATA)
    if not traj or not traj.get("interview"):
        return {"available": False, "reason": "no interview recorded"}
    ivr = traj["interview"]
    return {"available": True, "gaps": ivr["gaps"],
            "questions": ivr["questions"], "answers": ivr["answers"],
            "attestations": ivr["attestations"],
            "before": ivr["before"], "after": ivr["after"],
            "changed": ivr["before"]["recommendation"]
            != ivr["after"]["recommendation"]}


# ----------------------------------------------------------------------------
# The staged run — one generator, replay or live
# ----------------------------------------------------------------------------
def stream_run(nct_id: str, npi: str, mode: str = "replay"):
    """Yield (event, payload) per stage, in the order the screen shows them.

    In `live` mode this is a genuine run: it builds the rubric if one is
    missing, adjudicates, searches for comparable trials, scores participant
    experience, then generates interview questions and **stops** — the answers
    come from a human, so the run pauses rather than inventing them. Submitting
    answers to /api/interview resumes it.
    """
    live = mode == "live"

    def stage(key, label, status, data=None, detail=""):
        return ("stage", {"key": key, "label": label, "status": status,
                          "detail": detail, "data": data})

    yield stage("trial", "Trial information", "running")
    t = trial_stage(nct_id)
    yield stage("trial", "Trial information", "done", t,
                f"{t['n_population_defining']} population-defining criteria, "
                f"{t['n_screening']} screening excluded · burden from "
                f"{t['burden_source']}")

    yield stage("rubric", "Rubric generated for this trial", "running")
    if live and not (DATA / "rubrics" / f"{nct_id}.json").exists():
        info = _load(DATA / "trial_info" / f"{nct_id}.json")
        if info:
            rec = rubric.build(info, verbose=False)
            rubric.write(rec, DATA)
    r = rubric_stage(nct_id)
    yield stage("rubric", "Rubric generated for this trial",
                "done" if r.get("available") else "error", r,
                (f"{r['scoring']['n_criteria']} criteria, max "
                 f"{r['scoring']['max_score']} pts, "
                 f"{r['scoring']['public_coverage']:.0%} publicly verifiable")
                if r.get("available") else r.get("reason", ""))

    yield stage("evidence", "Evidence the matcher may use", "running")
    ev = evidence_stage(nct_id, npi)
    ex = ev["excluded"]
    yield stage("evidence", "Evidence the matcher may use", "done", ev,
                f"{ev['n_refs_available']} citable records across "
                f"{len(ev['sources'])} sources · excluded {ex['roles']} role(s) "
                f"and {ex['publications']} paper(s) about this trial")

    if mode == "live":
        yield stage("match", "Scoring against the rubric", "running")
        rubric_rec = _load(DATA / "rubrics" / f"{nct_id}.json")
        dossier = _load(DATA / "evidence" / f"{npi}.json")
        rep = matcher.match(rubric_rec, dossier, verbose=False)
        matcher.write(rep, DATA)
    m = match_stage(nct_id, npi)
    yield stage("match", "Scoring against the rubric",
                "done" if m.get("available") else "error", m,
                (f"{m['scoring']['recommendation']} — {m['scoring']['score']}/"
                 f"{m['scoring']['max_score']}, {m['scoring']['coverage']:.0%} "
                 f"coverage") if m.get("available") else m.get("reason", ""))

    yield stage("precedent", "What comparable trials did", "running")
    if live and not (DATA / "precedent" / f"{nct_id}.json").exists():
        info = _load(DATA / "trial_info" / f"{nct_id}.json")
        if info:
            precedent.write(precedent.find_similar(info), nct_id, DATA)
    p = precedent_stage(nct_id)
    yield stage("precedent", "What comparable trials did",
                "done" if p.get("available") else "skipped", p,
                precedent.headline(p))

    yield stage("patient", "Patient-experience proxy", "running")
    if live and not (DATA / "patient_proxy" / f"{nct_id}.json").exists() \
            and p.get("available"):
        manifest = _load(DATA / "trials_manifest.json")
        setting = ""
        if isinstance(manifest, list):
            setting = next((row.get("setting", "") for row in manifest
                            if row.get("nct_id") == nct_id), "")
        patient.write(patient.build_cohort(p, DATA, setting=setting,
                                           verbose=False), nct_id, DATA)
    pt = patient_stage(nct_id)
    yield stage("patient", "Patient-experience proxy",
                "done" if pt.get("available") else "skipped", pt,
                (f"median proxy {pt['median_proxy']}/100 across "
                 f"{pt['n_scored']} comparable trials with posted results")
                if pt.get("available") else pt.get("reason", ""))

    yield stage("interview", "Gap-closing interview", "running")
    if live and m.get("available"):
        rubric_rec = _load(DATA / "rubrics" / f"{nct_id}.json")
        dossier = _load(DATA / "evidence" / f"{npi}.json")
        rep = _load(DATA / "match_reports" / f"{nct_id}__{npi}.json")
        gaps = iv.find_gaps(rubric_rec, rep)
        questions, _ = (iv.generate_questions(rubric_rec, gaps)
                        if gaps else ([], {}))
        PENDING[f"{nct_id}__{npi}"] = {"gaps": gaps, "questions": questions}
        it = {"available": bool(questions), "awaiting_answers": True,
              "gaps": gaps, "questions": questions, "answers": {},
              "attestations": [], "before": m["scoring"], "after": m["scoring"]}
        yield stage("interview", "Gap-closing interview",
                    "done" if questions else "skipped", it,
                    (f"{len(gaps)} gaps — answer below to re-score"
                     if questions else "no gaps to close"))
        yield ("await", {"nct_id": nct_id, "npi": npi,
                         "questions": questions,
                         "scoring": m["scoring"],
                         "report_url": f"/report/{nct_id}__{npi}.html"})
        return
    it = interview_stage(nct_id, npi)
    yield stage("interview", "Gap-closing interview",
                "done" if it.get("available") else "skipped", it,
                (f"{len(it['gaps'])} gaps, {len(it['answers'])} answered")
                if it.get("available") else it.get("reason", ""))

    yield stage("rescore", "Re-scored with attestations", "running")
    rm = match_stage(nct_id, npi, variant="interviewed")
    if rm.get("available"):
        yield stage("rescore", "Re-scored with attestations", "done", rm,
                    f"{rm['scoring']['recommendation']} — "
                    f"{rm['scoring']['score']}/{rm['scoring']['max_score']}, "
                    f"{rm['scoring']['coverage']:.0%} coverage")
    else:
        yield stage("rescore", "Re-scored with attestations", "skipped", rm,
                    "no attestations recorded")

    final = rm if rm.get("available") else m
    traj = pipeline.load_trajectory(nct_id, npi, DATA)
    yield ("final", {
        "nct_id": nct_id, "npi": npi,
        "scoring": final.get("scoring"),
        "report_url": f"/report/{nct_id}__{npi}.html",
        "recorded": traj is not None,
        "live_seconds": (traj or {}).get("total_seconds"),
        "live_cost_usd": (traj or {}).get("total_cost_usd"),
    })


def resume_interview(nct_id: str, npi: str, answers: dict) -> dict:
    """Apply the physician's answers and re-adjudicate.

    The augmented dossier is never written over the public-only one: the
    before/after comparison has to stay reproducible, and a reader must be able
    to see what the score was before anyone was asked anything.
    """
    key = f"{nct_id}__{npi}"
    pend = PENDING.get(key)
    if not pend:
        return {"error": "no pending interview — run the pipeline first"}

    rubric_rec = _load(DATA / "rubrics" / f"{nct_id}.json")
    dossier = _load(DATA / "evidence" / f"{npi}.json")
    before = _load(DATA / "match_reports" / f"{key}.json")
    if not (rubric_rec and dossier and before):
        return {"error": "missing rubric, dossier or baseline match"}

    augmented, attestations = iv.apply_answers(dossier, pend["gaps"], answers)
    if not attestations:
        return {"error": "no answers supplied — nothing to re-score"}

    rescored = matcher.match(rubric_rec, augmented, verbose=False)
    rescored["variant"] = "interviewed"
    matcher.write(rescored, DATA)

    interview_rec = {
        "gaps": pend["gaps"], "questions": pend["questions"],
        "answers": answers, "attestations": attestations,
        "before": before["scoring"], "after": rescored["scoring"],
        "rescored": rescored,
    }
    html = report.render(before, rubric_rec, dossier,
                         precedent=precedent.load(nct_id, DATA),
                         patient_cohort=patient.load(nct_id, DATA),
                         interview=interview_rec)
    report.write(html, nct_id, npi, DATA)

    # Record the trajectory so this live run is replayable afterwards.
    traj = {
        "nct_id": nct_id, "npi": npi,
        "physician": rescored["physician"], "trial_label": rescored["trial_label"],
        "recommendation": rescored["scoring"]["recommendation"],
        "score": rescored["scoring"]["score"],
        "max_score": rescored["scoring"]["max_score"],
        "coverage": rescored["scoring"]["coverage"],
        "report_path": f"data/reports/{key}.html",
        "total_seconds": 0, "total_cost_usd": rescored["trace"]["cost_usd"],
        "steps": [], "artifacts": {},
        "interview": {k: interview_rec[k] for k in
                      ("gaps", "questions", "answers", "attestations",
                       "before", "after")},
    }
    pipeline.write_trajectory(traj, DATA)
    PENDING.pop(key, None)

    return {"ok": True,
            "before": before["scoring"], "after": rescored["scoring"],
            "changed": before["scoring"]["recommendation"]
            != rescored["scoring"]["recommendation"],
            "rescore": {"available": True, "scoring": rescored["scoring"],
                        "verdicts": rescored["verdicts"],
                        "narrative": rescored.get("narrative", "")},
            "attestations": attestations,
            "report_url": f"/report/{key}.html",
            "cost_usd": rescored["trace"]["cost_usd"]}


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):            # keep the console readable
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_POST(self) -> None:                                   # noqa: N802
        u = urlparse(self.path)
        if u.path != "/api/interview":
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            out = resume_interview(body.get("nct", ""), body.get("npi", ""),
                                   body.get("answers", {}) or {})
        except Exception:
            out = {"error": traceback.format_exc()[-500:]}
        self._json(out, 200 if out.get("ok") else 400)

    def do_GET(self) -> None:                                    # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            return

        if u.path == "/api/physicians":
            self._json(physicians_payload())
            return

        if u.path.startswith("/report/"):
            name = u.path.split("/report/", 1)[1]
            path = DATA / "reports" / name
            if not path.exists() or ".." in name:
                self._json({"error": "not found"}, 404)
                return
            self._send(200, path.read_bytes(), "text/html; charset=utf-8")
            return

        if u.path == "/api/evaluation":
            from . import evaluate
            self._json(evaluate.tier_separation(DATA))
            return

        if u.path == "/api/run":
            nct = (q.get("nct") or [""])[0]
            npi = (q.get("npi") or [""])[0]
            mode = (q.get("mode") or ["replay"])[0]
            if not (nct and npi):
                self._json({"error": "need nct and npi"}, 400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                for event, payload in stream_run(nct, npi, mode):
                    chunk = (f"event: {event}\n"
                             f"data: {json.dumps(payload)}\n\n").encode()
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                err = json.dumps({"error": traceback.format_exc()[-600:]})
                try:
                    self.wfile.write(f"event: error\ndata: {err}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    pass
            return

        self._json({"error": "not found"}, 404)


def serve(data_dir: Path, port: int = 8765) -> None:
    global DATA
    DATA = data_dir
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"  demo UI on http://127.0.0.1:{port}")
    print(f"  data: {data_dir.resolve()}")
    print("  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
        server.shutdown()
