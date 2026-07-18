# Clinical Trial Matching (Physician x Clinical Trial)

Matching physicians to clinical trials as potential **site investigators**, using
only public data, with every claim traceable to a source record.

Given a physician and a trial, it builds a rubric specific to that trial, scores
the physician against it from five public sources, asks the physician to close
whatever public data couldn't settle, and produces a report where every verdict
links to the record it rests on.

```
                    ┌── rubric ──┐
   trial record ────┤            ├──► adjudicate ──► interview ──► re-score ──► report
                    │  (per trial)         ▲          (asks)                      │
   physician ───────┴── evidence ──────────┘                                      │
   (5 public sources)                                                             │
                            comparable trials ──► precedent + patient proxy ──────┘
```

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # add your ANTHROPIC_API_KEY

# collect (no key needed, ~5 min)
python scripts/collect_trials.py          # 187 trials from ClinicalTrials.gov
python scripts/collect_physicians.py      # resolve investigators to NPIs
python scripts/collect_evidence.py        # 5-source dossiers + protocol PDFs

# score (needs key)
python scripts/build_rubrics.py --budget 2.00        # ~$0.86
python scripts/run_match.py --prebuild --budget 3.00 # ~$1.50

# the app
python scripts/serve_demo.py              # http://127.0.0.1:8765
```

Total spend for a full run: **under $3**.

---

## What it does, stage by stage

### 1 · Trial pool — `collect_trials.py`

Pulls candidate trials from the CT.gov v2 API and sorts them by how much
grounding material they carry.

```
12,660  breast cancer, any status
   186  + HR+/HER2- early-adjuvant narrowing
   187  pulled (incl. anchors)
```

| Bucket | n | For |
|---|---|---|
| gold | 17 | posted protocol PDF — the deep-grounding set |
| demo | 15 | recruiting — "a physician could join this today" |
| broad | 46 | breadth, for mining investigators |

The scoring weights a **posted protocol PDF at +3.0**, more than any other
signal, because the Schedule of Assessments inside it is the only honest source
of operational burden. Protocol availability skews hard toward *completed*
trials, which is why `gold` and `demo` are separate buckets rather than one
ranked list.

Scope is a parameter (`SCOPES` in `collect.py`), not a hardcoded assumption —
a new disease area is a new `Scope` and no other changes.

### 2 · Physicians — `collect_physicians.py`

Harvests investigator names off those trials and resolves them to real NPIs via
NPPES.

```
 79  distinct real-person names on 187 trials
 45  attempted
 17  resolved to a unique oncologist NPI
```

**Only ~40% resolve**, and the biggest cause isn't fixable: NPPES is a US-only
registry, so Aleix Prat (Barcelona), Michael Gnant (Vienna) and Masakazu Toi
(Kyoto) can never be matched through it.

Resolution **refuses to guess**. A same-name match is not proof of identity, and
a wrong pick doesn't fail loudly — it silently attributes one person's
publications and payments to another. Two false positives caught while building
this: *George Thomas Budd* → a **dentist**, *Louis WC Chow* → a **psychologist**.
An oncology taxonomy is now a hard requirement, and identity is keyed on **NPI,
never on name**.

Each physician gets a **graded trial bucket** with an expected-fit tier
(`known`/`strong`/`moderate`/`weak`) assigned from trial metadata. Nothing
downstream reads these — they're held out so the evaluation harness can check
afterwards whether real scores separate them.

### 3 · Evidence — `collect_evidence.py`

**Physician side**, five sources:

| Source | Answers |
|---|---|
| NPPES | who they are, what they're licensed as |
| PubMed | publication record — titles **and abstracts** |
| ClinicalTrials.gov | prior investigator roles |
| CMS Open Payments | has industry funded their research |
| CMS Medicare | visible clinical volume (a floor, never a total) |

Every record carries a ref that resolves to a public entry — `PMID:41812623`,
`NCT02513394`, `OpenPayments:1040131905`. **A claim with no ref is not
evidence.** Both CMS dataset ids are discovered at runtime rather than pinned in
config, since they change every program year.

**Trial side**: eligibility, arms, outcomes, sites — all from the API as
structured JSON. The PDF is used for **exactly one thing**: the Schedule of
Assessments, because visit burden is the one field the API doesn't have.

The judgment that matters here is the **eligibility split**. CT.gov returns one
free-text blob mixing inclusion and exclusion. We classify by meaning instead:

- *population-defining* — "histologically confirmed ER+/PR+, HER2−, Stage II".
  What patients the practice must already treat. **Becomes a requirement.**
- *screening* — ECOG status, lab thresholds, consent. Checked per enrolled
  patient. **Excluded** — it says nothing about whether a physician is a good site.

### 4 · The rubric — `build_rubrics.py`

The first LLM stage. In: one trial record. Out: a rubric tailored to that trial.

A rubric that would apply equally well to any oncology trial has failed — every
criterion must name what produced it in `derived_from`.

**Criteria span four dimensions**, each mapping to a different evidence source:

| Dimension | Answerable from |
|---|---|
| `expertise` | NPPES taxonomy, PubMed |
| `patient_population` | Medicare (weakly) — **mostly self-report** |
| `operational_capacity` | **mostly self-report** |
| `trial_execution` | CT.gov roles, Open Payments |

That asymmetry is the point: two dimensions are well served by public data and
two are barely served at all. Requiring at least one criterion per dimension
guarantees the rubric surfaces the gap rather than scoring only what's easy.

**Criticality** drives the weight: `hard_gate` (capping), `primary` (3 points),
`secondary` (1 point). The model assigns the *label*; code owns the *points*.

### 5 · Adjudication — `matcher.py`

The matcher has **no tools**. Its entire world is the rubric and the dossier
already on disk, so it cannot fetch — and therefore cannot invent — a fact
mid-judgment.

Two guards make the output falsifiable:

**Ref validation.** Every `evidence_refs` entry is checked against the refs the
dossier actually contains. A rationale citing a PMID that was never collected
fails validation rather than shipping as a citation.

**`unknown` is a real verdict.** The prompt draws a hard line between
`not_satisfied` (the evidence shows the criterion *isn't* met) and `unknown`
(nothing was collected either way), and forbids upgrading an absence into a
charitable `partial`.

**Target-trial exclusion.** For a physician genuinely on the trial being scored,
their dossier contains that trial — making the question circular. Both routes
are closed: the investigator role *and* the results papers they published about
it. Publications are filtered on **acronym or NCT id only**, never drug or
disease, since "has published on palbociclib in early breast cancer" is exactly
the expertise the rubric should credit. In production this is a **no-op** — a new
trial has no history to exclude.

**The four outcomes**, in precedence order:

```
gate failed?      → poor_fit               (nothing overrides this)
coverage < 50%?   → insufficient_evidence
score >= 70%?     → strong_fit
score >= 40%?     → possible_fit
otherwise         → poor_fit
```

`poor_fit` means **no**. `insufficient_evidence` means **we don't know**.
Collapsing those two would let the system reject people for being
un-Googleable rather than unsuitable.

### 6 · The interview — `interview.py`

Public data answers *"is this person a researcher"* well and *"does this person
treat THIS population"* badly. So the high-weight criteria come back `unknown`
and a genuinely good match lands at `insufficient_evidence`. That verdict is
correct — the fix is to ask.

Self-report is never laundered. Every attested record carries
`ref="attestation:<criterion_id>"`, a `[SELF-REPORTED]` prefix, and a badge
everywhere it appears. A `not_satisfied` verdict is deliberately **not** a gap —
re-asking would invite the subject to overturn a finding.

Measured on PALLAS × Erica Mayer: **10/18 possible fit (64% coverage) → 15/18
strong fit (100% coverage)**, with 6 of 11 criteria resting on attestation.

### 7 · Context — `precedent.py`, `patient.py`

Both deterministic, no model, no cost.

**Precedent** — what happened to comparable trials (same condition, phase,
intervention type). PALLAS: **81 comparable, 83% completed, 17% stopped early,
6% specifically because they couldn't enrol.** That last number is what a
prospective site actually wants, and it comes from classifying CT.gov's
free-text `whyStopped`.

**Operational outcome only** — CT.gov records whether a trial finished and
enrolled, not whether the treatment worked.

**Patient-experience proxy** — no public dataset records whether participants
were satisfied, so this composes what they *did*:

| Signal | Weight |
|---|---|
| Voluntary withdrawal (*"Withdrawal by Subject"* — they chose to leave) | 40% |
| Retention | 35% |
| Serious adverse events | 15% |
| Visit burden | 10% |

PALLAS's cohort: **17 trials scored, median 67.9, range 14.5–100**.

Two corrections the real data forced: **treat-until-progression** designs record
`COMPLETED = 0` because discontinuation *is* the endpoint, so retention is
dropped rather than scored as zero; and only trials that post results can be
scored at all, which biases the cohort toward trials that finished. The second
is reported, not corrected — correcting it would mean inventing numbers.

### 8 · Evaluation — `evaluate.py`

Sits **outside** the pipeline. Produces no score, changes no verdict. It's the
only thing that reads the held-out tier labels.

| Layer | Cost | Question |
|---|---|---|
| Tier separation | free | do scores rank `known` above `weak`? |
| Perturbation | ~$0.50 | does removing a source move the score? |
| Integrity | free | is each stored score reproducible from its verdicts? |

**Current: AUC 0.812** across 6 pairs — both `known` trials above both
negatives. Small n; treat as directional.

AUC is the pairwise (Mann-Whitney) form, verified identical to the ROC-curve
area and to `sklearn.roc_auc_score`.

No LLM judge: the most expensive layer and the least informative.

---

## Layout

```
trialfit/            the library — 20 modules, ~5,650 lines
├── ctgov.py         CT.gov v2 client
├── nppes.py         provider registry + identity resolution (refuses to guess)
├── pubmed.py        E-utilities: search, summaries, abstracts
├── cms.py           Open Payments + Medicare, runtime dataset discovery
├── collect.py       scope presets, scoring, bucketing
├── physicians.py    seed harvesting, roster, demo selection
├── buckets.py       per-physician trial buckets + expected-fit tiers
├── trialinfo.py     full trial detail + the eligibility split
├── protocol.py      PDF fetch, extraction, visit-grid location by structure
├── evidence.py      per-physician dossier across five sources
├── rubric.py        evaluator agent — per-trial rubric + scoring math
├── matcher.py       adjudication, ref validation, target-trial exclusion
├── interview.py     gap-closing interview, self-report, re-score
├── precedent.py     comparable-trial outcomes (no LLM)
├── patient.py       participant-experience proxy (no LLM)
├── evaluate.py      tier separation · perturbation · integrity
├── report.py        self-contained printable HTML report
├── pipeline.py      end-to-end run + trajectory record/replay
├── server.py        stdlib demo server, SSE stage streaming
└── ui.html          the front end — no framework, no build

scripts/             6 thin CLIs over the library
data/                everything generated
```

**Filenames are the join keys.** `{NCT}` links trial-side artifacts, `{NPI}`
physician-side, `{NCT}__{NPI}` a pair. No database — the filesystem is the schema.

---

## The demo

A live run makes several model calls; that's most of a short demo spent watching
a terminal. So every run writes a **trajectory** — the finished artifacts plus
the per-step timings the live run actually took.

```bash
python scripts/run_match.py --list                      # what's recorded
python scripts/run_match.py --replay --nct NCT02513394 --npi 1336121789
```

Replay re-emits those steps from disk with **zero API calls**, paced so the
audience sees the pipeline progress. What replays is a real run's output, not a
mock.

The UI has both modes. **Live is genuinely live** — it builds a missing rubric,
adjudicates, searches CT.gov, then generates interview questions and *pauses*,
because the answers have to come from a person. You type them in the browser,
hit re-score, and the report is rebuilt and a trajectory written — so a live run
becomes replayable afterwards.

`data/demo_config.json` scopes the UI to one physician and a fixed trial set.
Delete it to get everything back.

---

## Configuration

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | **Required** for the scoring stages |
| `TRIALFIT_MODEL` | default `claude-sonnet-5` |
| `TRIALFIT_EFFORT` | default `high` |
| `NCBI_API_KEY` | optional — raises PubMed's rate limit 3/s → 10/s |

`.env` is loaded automatically and git-ignored; a real environment variable
overrides it.

**Cost per trial** (~5k in, ~4k out): Sonnet 5 **$0.045**, Opus 4.8 $0.114.
Note Opus 4.7 and 4.8 are priced identically ($5/$25 per MTok) — stepping down a
version saves nothing; the saving is the Sonnet tier.

Every artifact records its own `trace.cost_usd`, and `--budget N` stops a run
once it has spent that much.
