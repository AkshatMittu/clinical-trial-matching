"""
report.py — render one match as a self-contained, printable report.

Output is a single HTML file with no external references: no CDN, no webfont,
no image host. It opens from `file://`, survives being emailed as an
attachment, and prints to PDF from any browser (Cmd/Ctrl-P, or the button in
the header). That beats generating a PDF directly here — it needs no extra
dependency, and the same artifact is both the on-screen view and the download.

What the report has to show, and why:

  * **The rubric it was scored against.** A score without the rubric is an
    opinion. Every criterion appears with its requirement, its `satisfies_if`,
    and its weight.
  * **The evidence behind every verdict.** Each rationale carries the refs it
    cited, rendered as links to the actual public record — PubMed, CT.gov,
    NPPES — so a reader can check any claim rather than trusting it.
  * **What is self-reported.** Attested criteria are marked, everywhere they
    appear. A reader must never have to guess whether a line came from a public
    record or from the physician.
  * **Precedent.** What happened to similar trials, with the operational-only
    caveat attached rather than buried.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

REC_STYLE = {
    "strong_fit": ("#0f7b3f", "#e8f5ee", "Strong fit"),
    "possible_fit": ("#8a6100", "#fdf3e0", "Possible fit"),
    "poor_fit": ("#a32020", "#fdeaea", "Poor fit"),
    "insufficient_evidence": ("#5a5a68", "#f0f0f3", "Insufficient evidence"),
}
VERDICT_STYLE = {
    "satisfied": ("#0f7b3f", "#e8f5ee", "satisfied"),
    "partial": ("#8a6100", "#fdf3e0", "partial"),
    "not_satisfied": ("#a32020", "#fdeaea", "not satisfied"),
    "unknown": ("#5a5a68", "#f0f0f3", "unknown"),
}


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def ref_link(ref: str) -> str:
    """Turn a ref into a link to the public record it names."""
    r = str(ref)
    if r.startswith("PMID:"):
        pid = r.split(":", 1)[1]
        return f'<a href="https://pubmed.ncbi.nlm.nih.gov/{e(pid)}/">{e(r)}</a>'
    if re.fullmatch(r"NCT\d{8}", r):
        return f'<a href="https://clinicaltrials.gov/study/{e(r)}">{e(r)}</a>'
    if r.startswith("NPI:"):
        npi = r.split(":", 1)[1]
        return (f'<a href="https://npiregistry.cms.hhs.gov/provider-view/'
                f'{e(npi)}">{e(r)}</a>')
    if r.startswith("attestation:"):
        return f'<span class="attest">{e(r)}</span>'
    return f"<code>{e(r)}</code>"


CSS = """
:root{--ink:#1a1a1f;--mut:#5a5a68;--line:#e2e2e8;--bg:#fff;--accent:#2a4d8f}
*{box-sizing:border-box}
body{margin:0;background:#f4f4f7;color:var(--ink);
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.page{max-width:930px;margin:0 auto;background:var(--bg);padding:38px 46px 60px}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);
 margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:14px;margin:18px 0 6px}
.sub{color:var(--mut);font-size:14px;margin:0 0 20px}
.hdr{display:flex;justify-content:space-between;align-items:flex-start;gap:24px}
.badge{display:inline-block;padding:5px 13px;border-radius:20px;font-weight:600;
 font-size:13px;white-space:nowrap}
.verdict-badge{display:inline-block;padding:2px 9px;border-radius:11px;
 font-size:11.5px;font-weight:600;white-space:nowrap}
.scorebox{text-align:right;min-width:190px}
.scorebox .n{font-size:32px;font-weight:650;letter-spacing:-.02em;line-height:1.1}
.scorebox .l{color:var(--mut);font-size:12.5px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}
.stat{border:1px solid var(--line);border-radius:7px;padding:11px 13px}
.stat .v{font-size:19px;font-weight:600}
.stat .k{color:var(--mut);font-size:11.5px;text-transform:uppercase;
 letter-spacing:.04em;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;
 color:var(--mut);border-bottom:1px solid var(--line);padding:7px 9px;font-weight:600}
td{padding:9px;border-bottom:1px solid #f0f0f4;vertical-align:top}
tr:last-child td{border-bottom:none}
.crit{font-weight:600}
.meta{color:var(--mut);font-size:11.5px}
.sat{font-style:italic;color:var(--mut);font-size:12.5px;margin-top:3px}
.refs{margin-top:5px;font-size:11.5px}
.refs a,.refs code{margin-right:7px;color:var(--accent);text-decoration:none}
.refs a:hover{text-decoration:underline}
.attest{background:#fff4d6;border:1px solid #e8d08a;border-radius:3px;
 padding:1px 5px;font-size:11px;color:#7a5c00;font-weight:600}
.note{background:#f7f7fa;border-left:3px solid var(--line);padding:11px 14px;
 margin:14px 0;font-size:13px;color:var(--mut);border-radius:0 5px 5px 0}
.warn{background:#fdf3e0;border-left-color:#e0a83c;color:#6b4e00}
.delta{display:flex;gap:26px;align-items:center;flex-wrap:wrap;
 background:#f7f7fa;border-radius:7px;padding:14px 18px;margin:12px 0}
.delta .arm{font-size:12.5px;color:var(--mut)}
.delta .arm b{display:block;font-size:17px;color:var(--ink);font-weight:650}
.arrow{font-size:20px;color:var(--mut)}
.qa{border:1px solid var(--line);border-radius:7px;padding:12px 15px;margin:9px 0}
.qa .q{font-weight:600;font-size:13.5px}
.qa .a{margin-top:6px;padding-left:12px;border-left:2px solid #e8d08a;
 font-size:13.5px;color:#3a3a45}
.qa .why{color:var(--mut);font-size:12px;margin-top:3px}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
 color:var(--mut);font-size:11.5px;line-height:1.6}
.btn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:7px 14px;
 font-size:13px;cursor:pointer;color:var(--ink)}
.btn:hover{background:#f4f4f7}
@media print{
 body{background:#fff}
 .page{max-width:none;padding:0}
 .noprint{display:none!important}
 h2{page-break-after:avoid}
 tr,.qa,.stat{page-break-inside:avoid}
 a{color:var(--ink);text-decoration:none}
}
"""


def _stat(value, label) -> str:
    return f'<div class="stat"><div class="v">{e(value)}</div>' \
           f'<div class="k">{e(label)}</div></div>'


def _verdict_rows(verdicts: list[dict], attested: set[str]) -> str:
    order = {"hard_gate": 0, "primary": 1, "secondary": 2}
    rows = []
    for v in sorted(verdicts, key=lambda x: (order.get(x.get("criticality"), 3),
                                             x.get("dimension", ""))):
        col, bg, label = VERDICT_STYLE.get(v["verdict"], VERDICT_STYLE["unknown"])
        gate = ' <span class="attest">HARD GATE</span>' \
            if v.get("criticality") == "hard_gate" else ""
        att = ' <span class="attest">SELF-REPORTED</span>' \
            if v["criterion_id"] in attested else ""
        refs = " ".join(ref_link(r) for r in v.get("evidence_refs", []))
        rows.append(f"""<tr>
 <td style="width:31%">
   <div class="crit">{e(v.get('requirement', v['criterion_id']))}</div>
   <div class="meta">{e(v.get('dimension', ''))} · {e(v.get('criticality', ''))}
     · <code>{e(v['criterion_id'])}</code>{gate}{att}</div>
   <div class="sat">satisfies if: {e(v.get('satisfies_if', ''))}</div>
 </td>
 <td style="width:13%"><span class="verdict-badge"
   style="color:{col};background:{bg}">{e(label)}</span></td>
 <td>{e(v.get('rationale', ''))}
   {f'<div class="refs">{refs}</div>' if refs else ''}</td>
</tr>""")
    return "\n".join(rows)


def _precedent_block(prec: Optional[dict]) -> str:
    if not prec or not prec.get("available"):
        reason = (prec or {}).get("reason", "not collected")
        return f'<div class="note">No comparable-trial precedent available ' \
               f'({e(reason)}).</div>'
    thin = ""
    if not prec.get("sample_adequate", True) and prec.get("sample_note"):
        thin = f'<div class="note warn">{e(prec["sample_note"])}</div>'
    reasons = prec.get("stop_reasons") or {}
    reason_rows = "".join(
        f"<tr><td>{e(k.replace('_', ' '))}</td><td>{e(v)}</td></tr>"
        for k, v in reasons.items())
    return f"""
<div class="grid">
  {_stat(f"{prec['completion_rate']:.0%}", "completed")}
  {_stat(f"{prec['stopped_early_rate']:.0%}", "stopped early")}
  {_stat(f"{prec['accrual_failure_rate']:.0%}", "stopped: could not enrol")}
  {_stat(prec['n_similar'], "comparable trials")}
</div>
{thin}
<p style="font-size:13.5px">Matched on condition <b>{e(prec['query']['condition'])}</b>,
phase {e('/'.join(prec['query']['phases']) or 'any')}, intervention type
{e('/'.join(prec['query']['intervention_types']) or 'any')}.
Median enrolment: {e(prec.get('median_enrollment_completed'))} for trials that
completed vs {e(prec.get('median_enrollment_stopped'))} for trials that stopped.</p>
{f'<h3>Why comparable trials stopped</h3><table>{reason_rows}</table>' if reason_rows else ''}
<div class="note warn"><b>Operational outcome only.</b> {e(prec['caveat'])}</div>
"""


def _interview_block(iv: Optional[dict]) -> str:
    if not iv or not iv.get("questions"):
        return ""
    before, after = iv["before"], iv["after"]
    changed = before["recommendation"] != after["recommendation"]
    bcol, bbg, blab = REC_STYLE.get(before["recommendation"], REC_STYLE["poor_fit"])
    acol, abg, alab = REC_STYLE.get(after["recommendation"], REC_STYLE["poor_fit"])

    qa = ""
    for q in iv["questions"]:
        ans = (iv.get("answers") or {}).get(q["criterion_id"], "")
        skipped = not ans or ans.strip().lower() == "skip"
        qa += f"""<div class="qa">
  <div class="q">{e(q['question'])}</div>
  <div class="why">{e(q.get('why', ''))} · <code>{e(q['criterion_id'])}</code></div>
  <div class="a">{'<i>Not answered — criterion left unresolved.</i>'
                  if skipped else e(ans)}</div>
</div>"""

    delta = f"""<div class="delta">
  <div class="arm">Before (public data only)
    <b><span class="badge" style="color:{bcol};background:{bbg}">{e(blab)}</span></b></div>
  <div class="arrow">&rarr;</div>
  <div class="arm">After interview
    <b><span class="badge" style="color:{acol};background:{abg}">{e(alab)}</span></b></div>
  <div class="arm">Score <b>{e(before['score'])} &rarr; {e(after['score'])}</b>
    / {e(after['max_score'])}</div>
  <div class="arm">Coverage
    <b>{before['coverage']:.0%} &rarr; {after['coverage']:.0%}</b></div>
</div>"""

    banner = ("" if changed else
              '<div class="note">The interview raised coverage but did not change '
              'the recommendation.</div>')
    return f"""<h2>Gap-closing interview</h2>
<p style="font-size:13.5px">Public records could not settle
{len(iv['gaps'])} criteria. These were put to the physician directly; the answers
are recorded below as <span class="attest">SELF-REPORTED</span> and weighed as
testimony, not as public evidence.</p>
{delta}{banner}{qa}"""


def _patient_block(pc: Optional[dict]) -> str:
    if not pc or not pc.get("available"):
        return ('<div class="note">No participant-experience data available '
                f'({e((pc or {}).get("reason", "not collected"))}).</div>')
    rows = ""
    for t in pc["trials"]:
        f = t.get("participant_flow") or {}
        ret = ('<span class="meta">n/a — treat-until-progression</span>'
               if f.get("retention_interpretable") is False
               else (f"{f['retention_rate']:.0%}"
                     if f.get("retention_rate") is not None else "—"))
        rows += (f"<tr><td><code>{e(t['nct_id'])}</code> {e(t['label'])}"
                 f"<div class='meta'>{e(t['status'])}</div></td>"
                 f"<td><b>{e(t['experience_proxy'])}</b></td><td>{ret}</td>"
                 f"<td>{f.get('voluntary_withdrawal_rate', 0):.1%}</td></tr>")
    weights = ", ".join(f"{k.replace('_', ' ')} {v:.0%}"
                        for k, v in (pc.get("weights") or {}).items())
    return f"""
<div class="grid">
  {_stat(pc['median_proxy'], "median proxy /100")}
  {_stat(pc['n_scored'], "trials scored")}
  {_stat(f"{pc['min_proxy']}-{pc['max_proxy']}", "range")}
  {_stat(pc.get('n_skipped_no_results', 0), "skipped: no results")}
</div>
<table><thead><tr><th>Trial</th><th>Proxy</th><th>Retention</th>
 <th>Voluntary withdrawal</th></tr></thead><tbody>{rows}</tbody></table>
<div class="note warn"><b>A proxy, not satisfaction.</b> {e(pc['caveat'])}
 Weights: {e(weights)}.</div>"""


def render(match: dict, rubric_rec: dict, dossier: dict,
           precedent: Optional[dict] = None,
           patient_cohort: Optional[dict] = None,
           interview: Optional[dict] = None) -> str:
    """Build the full HTML report for one adjudicated pair."""
    final = interview["rescored"] if (interview and interview.get("rescored")) \
        else match
    sc = final["scoring"]
    col, bg, label = REC_STYLE.get(sc["recommendation"], REC_STYLE["poor_fit"])
    attested = {a["criterion_id"] for a in (interview or {}).get("attestations", [])}

    src_rows = "".join(
        f"<tr><td>{e(en['source'])}</td><td>{e(en['status'])}</td>"
        f"<td>{e(en['summary'])}</td></tr>"
        for en in dossier.get("entries", []))

    dim_rows = "".join(
        f"<tr><td>{e(d.replace('_', ' '))}</td><td>{e(v['n'])}</td>"
        f"<td>{'—' if v.get('pct') is None else format(v['pct'], '.0%')}</td></tr>"
        for d, v in sc.get("by_dimension", {}).items())

    prov = rubric_rec.get("provenance", {})
    burden = prov.get("burden_source", "unknown")
    burden_note = (
        f"Site burden was read from the posted protocol's Schedule of "
        f"Assessments (page {prov.get('soa_page')}, "
        f"confidence: {prov.get('soa_confidence')})."
        if burden == "protocol" else
        "No protocol was posted for this trial, so site burden was "
        "<b>inferred</b> from phase, design and interventions rather than read "
        "from a visit schedule.")

    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(final['physician'])} &times; {e(final['nct_id'])} — investigator fit</title>
<style>{CSS}</style></head><body><div class="page">

<div class="hdr">
 <div>
  <h1>{e(final['physician'])} &times; {e(final['trial_label'] or final['nct_id'])}</h1>
  <p class="sub">Site-investigator fit assessment ·
   <a href="https://clinicaltrials.gov/study/{e(final['nct_id'])}">{e(final['nct_id'])}</a>
   · NPI {e(final['npi'])} · {e(final.get('specialty', '').replace('_', ' '))}</p>
  <span class="badge" style="color:{col};background:{bg}">{e(label)}</span>
  <span class="meta" style="margin-left:9px">{e(sc['reason'])}</span>
 </div>
 <div class="scorebox">
  <div class="n">{e(sc['score'])}<span style="font-size:19px;color:var(--mut)">
   /{e(sc['max_score'])}</span></div>
  <div class="l">{sc['score_pct']:.0%} of available points</div>
  <div class="l">{sc['coverage']:.0%} of criteria adjudicated</div>
  <button class="btn noprint" style="margin-top:9px"
   onclick="window.print()">Save as PDF</button>
 </div>
</div>

<div class="grid">
 {_stat(sc['n_criteria'], "criteria")}
 {_stat(sc['verdict_counts'].get('satisfied', 0), "satisfied")}
 {_stat(sc['verdict_counts'].get('partial', 0), "partial")}
 {_stat(sc['verdict_counts'].get('unknown', 0), "unknown")}
</div>

{f'<div class="note warn"><b>Hard gate not met:</b> {e(", ".join(sc["gate_failures"]))}. A failed gate caps the assessment regardless of the other criteria.</div>' if sc.get('gate_failures') else ''}

<h2>Assessment</h2>
<p>{e(final.get('narrative', ''))}</p>

<h2>What comparable trials did</h2>
{_precedent_block(precedent)}

<h2>Participant experience in those trials</h2>
{_patient_block(patient_cohort)}

{_interview_block(interview)}

<h2>Rubric and evidence</h2>
<p style="font-size:13.5px">This rubric was generated for
<b>{e(final['nct_id'])}</b> specifically. Weights: hard gate (capping),
primary = 3 points, secondary = 1 point. Every rationale below cites the public
records it rests on.</p>
<table>
 <thead><tr><th>Criterion</th><th>Verdict</th><th>Rationale and evidence</th></tr></thead>
 <tbody>{_verdict_rows(final['verdicts'], attested)}</tbody>
</table>

<h3>By dimension</h3>
<table><thead><tr><th>Dimension</th><th>Criteria</th><th>Score</th></tr></thead>
<tbody>{dim_rows}</tbody></table>

<h2>Evidence sources searched</h2>
<table><thead><tr><th>Source</th><th>Result</th><th>Summary</th></tr></thead>
<tbody>{src_rows}</tbody></table>

<h2>Target patient and site burden</h2>
<p><b>Target patient.</b> {e(rubric_rec['rubric']['target_patient'])}</p>
<p><b>Site burden.</b> {e(rubric_rec['rubric']['site_burden'])}</p>
<div class="note">{burden_note}</div>

<footer>
 <b>Scope.</b> Public data only; no PHI. This is a research prototype, not
 medical advice and not investigator advice. Sources: ClinicalTrials.gov, NPPES,
 PubMed, CMS Open Payments, CMS Medicare Physician &amp; Other Practitioners.<br>
 <b>Known limits.</b> PubMed author search matches surname plus initial and can
 include namesakes. ClinicalTrials.gov investigator matching is full-text;
 confirmed roles are separated from weaker text-only mentions. Medicare
 utilisation reflects Medicare claims only and is a floor on practice volume,
 never a total. Criteria marked
 <span class="attest">SELF-REPORTED</span> rest on the physician's own
 attestation rather than a public record.<br>
 <b>Scoring.</b> Verdicts are the model's judgment; the score, gate logic,
 coverage and recommendation are computed in code from those verdicts —
 the model cannot emit a score. Thresholds: strong &ge;
 {sc['thresholds']['strong']:.0%}, possible &ge;
 {sc['thresholds']['possible']:.0%}, minimum coverage
 {sc['thresholds']['min_coverage']:.0%}.<br>
 Generated {e(gen)} · rubric model {e(rubric_rec.get('model'))} ·
 adjudication model {e(final.get('model'))}
</footer>
</div></body></html>"""


def write(html_text: str, nct_id: str, npi: str, data_dir: Path,
          suffix: str = "") -> Path:
    out_dir = data_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{nct_id}__{npi}{suffix}.html"
    path.write_text(html_text)
    return path
