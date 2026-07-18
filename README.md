# trial_fit

Matching physicians to clinical trials as potential **site investigators**, using
only public data, with every claim traceable to a source record.

Built step by step. This is where we are:

- [x] **Step 1 — trial pool** (`scripts/collect_trials.py`)
- [x] **Step 2 — physicians + their trial buckets** (`scripts/collect_physicians.py`)
- [x] **Step 3 — evidence, both sides** (`scripts/collect_evidence.py`)
- [x] **Step 4 — evaluator agent → per-trial rubric** (`scripts/build_rubrics.py`)
- [x] **Step 5 — match, interview, precedent, report** (`scripts/run_match.py`)
- [x] **Step 6 — evaluation harness** (`trialfit/evaluate.py`)
- [x] **Step 7 — patient-experience proxy** (`trialfit/patient.py`)
- [x] **Step 8 — demo UI** (`scripts/serve_demo.py`)

## Step 1 — the sample trial set

```bash
pip install -r requirements.txt
python scripts/collect_trials.py --recon    # funnel sizes, pulls nothing
python scripts/collect_trials.py            # ~2 min, no API key needed
```

Pulls candidate trials from the ClinicalTrials.gov v2 API, flattens the metadata
that decides whether a trial is good demo material, scores it, and sorts it into
buckets.

### What we have

Scope `breast_hr_pos` — HR+/HER2− early/adjuvant breast cancer. The funnel:

```
  12,660   breast cancer, any status
     186   + HR+/HER2− early-adjuvant term
     187   pulled (incl. anchors)
```

| Bucket | n | What it's for |
|---|---|---|
| **gold** | 17 | Posted protocol PDF + rich eligibility. The deep-grounding set. |
| **demo** | 15 | Recruiting. The "a physician could join this today" story. |
| **broad** | 46 | Breadth, for mining historical investigators. |
| other | 83 | Off-target; ignored downstream. |

Verified: **17/17 gold trials have a named official and a reachable protocol PDF**
(1–9 MB each). Both prototype anchors are present — PALLAS (`NCT02513394`, gold)
and CAMBRIA-2 (`NCT05952557`, demo).

### Why these buckets

A trial is good demo material mostly because of **one field: a posted protocol
PDF.** The Schedule of Assessments inside it is the only honest source of
operational burden — what a site actually has to do per visit. Without it, burden
has to be inferred from phase and design, which is a guess. That's why the score
weights it +3.0, more than any other signal, and why `gold` requires it.

The catch: protocol availability skews hard toward **completed** trials. Recruiting
trials usually have nothing posted yet. So the set that grounds best and the set
that tells the best live story are disjoint — hence `gold` and `demo` as separate
buckets rather than one ranked list.

### Output

```
data/
├── trials_raw/{NCT}.json      full v2 records (187, ~14 MB) — git-ignored, refetchable
├── trials_manifest.csv        one flat row per trial — the working index
├── trials_manifest.json       same, for downstream code
├── trials_{gold,demo,broad}_ids.txt
└── collection_summary.json    funnel + bucket counts
```

The manifest and id lists are committed; raw records are not (they rebuild in one
command). Without the manifest, a checkout can't reproduce *which* trials we chose.

### Quality score

```
+3.0  posted protocol PDF        the big one — real grounding material
+2.0  eligibility > 1500 chars   (or +1.0 if > 600)
+1.0  posted results
+1.0  >= 1 named official
+1.0  >= 10 study sites          (or +0.5 if >= 1)
+1.0  enrollment >= 100
+1.0  phase 3                    (or +0.5 phase 2)
```

## Step 2 — physicians, and a graded trial bucket for each

```bash
python scripts/collect_physicians.py --seeds-only   # harvested names, no calls
python scripts/collect_physicians.py                # resolve + build buckets
```

The pipeline is **physician-centric**: a physician is the subject, and their
bucket is the set of trials we ask *"would this person be a good site
investigator?"*

### The three demo physicians

| Physician | NPI | Specialty | Trials |
|---|---|---|---|
| Erica Mayer | `1336121789` | Medical Oncology · Boston, MA | 4 listed (3 gold) |
| Coral Omene | `1861688988` | Hematology & Oncology · New York, NY | 2 listed |
| Laura Esserman | `1679537971` | Surgical Oncology · San Francisco, CA | 1 listed |

Selected for **gold-trial overlap** (a posted protocol PDF means requirements
come from a real Schedule of Assessments, not from phase and design) and for
**specialty spread** — which is what makes the scores differ for real reasons.

### Buckets are graded, on purpose

If every trial in a bucket were an obvious fit, the scores would all cluster high
and tell us nothing about whether the scorer works. So each trial gets an
expected tier from public signals *before* any scoring:

| Tier | Meaning | Expect |
|---|---|---|
| `known` | this physician is a listed official on the trial | high |
| `strong` | right subtype, right setting, specialty matches the interventions | high |
| `moderate` | in scope, but one axis is off | mid |
| `weak` | off-subtype, wrong setting, or specialty mismatch | low |

**34 pairs across 3 physicians** — 7 known, 9 strong, 9 moderate, 9 weak.

The tier is a **prediction, not an input.** Nothing downstream reads it while
scoring. It exists so that afterwards we can ask whether the scores actually
separate the tiers. If `weak` trials score as high as `known` ones, the scorer
isn't measuring fit — and we want that to come out of the data rather than be
assumed.

### The buckets really are physician-specific

The same trial lands in a different tier depending on who's being scored:

| Trial | Mayer (med onc) | Omene (heme onc) | Esserman (surgical) |
|---|---|---|---|
| PALLAS `NCT02513394` | **known** | strong | moderate |
| SOLAR-1 `NCT02437318` | strong | strong | moderate |
| Metformin `NCT01101438` | moderate | moderate | **weak** |

Esserman is a surgical oncologist, so pure-`DRUG` trials like PALLAS and SOLAR-1
drop for her, while `DRUG|PROCEDURE` and `DRUG|RADIATION` trials rise into her
strong tier. This is the point: it's one physician-specific ranking, not a global
trial ranking wearing three different hats.

### Resolution refuses to guess

A same-name NPPES match is **not** proof of identity, and a wrong pick doesn't
fail loudly — it silently attributes one person's publications and payments to
another. So `nppes.resolve()` returns `resolved` / `ambiguous` / `not_found`, and
only resolves when exactly one candidate survives filtering.

Two false positives caught while building this, both from accepting a lone
same-name hit:

- *George Thomas Budd* → a **dentist** in Lumberton, NJ
- *Louis WC Chow* → a **psychologist** in Boston, MA

The fix: an oncology taxonomy is a hard requirement, not a preference. Identity
is keyed on **NPI, never on name**, which collapsed "Erica Mayer" and "Erica L.
Mayer" into one physician.

### The seed is lossy, and that's the honest result

```
   79   distinct real-person names on 187 trials
   45   attempted
   17   resolved to a unique oncologist NPI      <- the roster
   27   not_found
    1   merged (same NPI, two name spellings)
```

**Only ~40% of attempted names resolve**, and the largest cause isn't fixable:
**NPPES is a US-only registry.** Aleix Prat (Barcelona), Michael Gnant (Vienna),
Masakazu Toi (Kyoto) and Eva Ciruelos (Madrid) lead major breast trials and can
never be resolved through it. With the corporate placeholders filtered at the
seed stage, usable yield is roughly **1 in 5 listed officials**.

### Output

```
data/
├── physicians/{NPI}.json      one record per physician
├── physicians_roster.json     all 17 — the working index
├── physicians_summary.json    resolution funnel + demo picks
├── buckets/{NPI}.json         one graded trial bucket per demo physician
├── scoring_pairs.json         flat (physician, trial, tier) work list
└── buckets_summary.json       tier totals
```

## Step 3 — the evidence both sides need

```bash
python scripts/collect_evidence.py               # both sides
python scripts/collect_evidence.py --physicians  # physician side only
python scripts/collect_evidence.py --trials      # trial side only
```

No API key, no LLM. This is deterministic collection — the agents in later steps
reason over what's gathered here and may only cite what appears in it.

### Physician side — five sources

| Source | Question it answers |
|---|---|
| NPPES | who they are, what they're licensed as |
| PubMed | do they publish in the disease area, and recently |
| ClinicalTrials.gov | have they held investigator roles before |
| CMS Open Payments | has industry paid them for *research* before |
| CMS Medicare | how much clinical volume is visible |

Collected, and genuinely differentiating:

| Physician | PubMed (5y) | CT.gov roles | Open Payments | Medicare |
|---|---|---|---|---|
| Erica Mayer | 117 (60) | 11 confirmed | $2,883 / 4 records | 206 svc, 5 codes |
| Coral Omene | 20 (12) | 2 confirmed | none | 192 svc, 4 codes |
| Laura Esserman | 370 (90) | 5 confirmed | none | 118 svc, 6 codes |

Every record carries a ref that resolves to a public entry — `PMID:41812623`,
`NCT02513394`, `OpenPayments:1040131905`. **A claim with no ref is not evidence.**

This layer is a **collector, not a judge**: it records what each source returned,
including when a source returned nothing, and never forms a view on fit.

**Both CMS dataset ids are discovered at runtime** from the CMS metastore rather
than pinned in `.env`. They change every program year, so configuration would go
stale silently. One gotcha worth recording: the Open Payments query path is
`/datastore/query/{distribution_id}` with **no** trailing `/0` — the indexed form
404s for these datasets.

### Trial side — what a requirement can be derived from

Step 1's manifest is a *selection* index. Matching needs full text that a
requirement can cite: eligibility, arms, interventions, outcomes, sites,
officials, and the protocol's Schedule of Assessments.

**The eligibility split** is the judgment that matters here. Eligibility mixes
two different kinds of criteria, and only one bears on physician fit:

- *population-defining* — "histologically confirmed ER+/PR+, HER2−, Stage II".
  What patients the practice must already treat. **These become requirements.**
- *per-patient screening* — ECOG status, lab thresholds, washout, consent.
  Checked per enrolled patient; says nothing about whether a physician is a good
  site. **These are excluded.**

On PALLAS: 10 population-defining, 10 screening, 16 unclear. The split is a
heuristic and is recorded as such, so a later stage can review it rather than
inherit it silently.

### Operational burden, and how confident we are about it

**12 of 19** trials have a posted protocol. For those, burden comes from the
Schedule of Assessments; for the rest it can only be inferred from phase and
design, and is flagged `inferred` rather than presented as read from source.

Locating the grid is harder than it looks. Matching the heading text finds the
*words* "Schedule of Assessments" — which appear in cross-references throughout
a protocol's body text. In PALLAS that put the extractor on page 49, a paragraph
mentioning the schedule, while the actual grid was on page 58.

So pages are ranked by **grid structure** — density of standalone `X` marks
crossed with visit-column vocabulary (Screening, Cycle *n*, Day *n*, End of
Treatment) — not by heading text. Results carry their confidence:

| Confidence | n | Meaning |
|---|---|---|
| `grid` | 6 | page is structurally a visit table |
| `heading_only` | 5 | heading matched, no table structure — may be a reference |
| absent | 1 | protocol posted, no schedule located |

`heading_only` usually means the table's marks didn't survive PDF text
extraction. Labelling it beats silently passing prose off as a visit schedule.

### Output

```
data/
├── evidence/{NPI}.json      per-physician dossier — 5 sources, refs, gaps
├── trial_info/{NCT}.json    eligibility split, arms, outcomes, sites, SoA
├── protocols/{NCT}_protocol.pdf   downloaded protocols (git-ignored)
└── cache/{NCT}_pages.json         extracted page text (git-ignored)
```

### The gap public data cannot close

Public sources are good at *"is this person a researcher"* and bad at *"does this
person treat THIS patient population"*. Medicare volume is the only population
signal available and it is Medicare-only — a floor, not a total. Each dossier
records this in `gaps` rather than papering over it. Closing it needs the
physician's own attestation, which is what the interview step is for.

## Step 4 — the evaluator agent

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/build_rubrics.py --dry-run          # show the brief, no API call
python scripts/build_rubrics.py --only NCT02513394
python scripts/build_rubrics.py                    # all 19 trials
```

The first LLM stage. In: one trial record. Out: a **rubric tailored to that
trial** — the criteria a physician should be scored against for *this* study.

### Why per-trial and not one checklist

A first-in-human dose-escalation study needs a site that can handle intensive PK
sampling and DLT review. A 400-site adjuvant phase 3 needs throughput and a large
eligible population. Scoring both against one fixed checklist would measure the
checklist, not the fit. **A rubric that would apply equally well to any oncology
trial has failed** — every criterion must name what in the record produced it, in
`derived_from`.

### Three constraints, each load-bearing

**1. The agent has no tools.** Step 3 already assembled the trial record; the
agent gets a brief built deterministically by `build_brief()` and nothing else.
It cannot fetch, and therefore cannot invent, a fact mid-generation. (PALLAS's
brief is ~2,700 tokens.)

**2. Python owns every number.** The model assigns each criterion a *criticality
label* — `hard_gate` / `primary` / `secondary` — and `WEIGHTS` turns labels into
points (`primary=3`, `secondary=1`). The model never emits a weight or a total,
so it cannot tune the arithmetic to reach a score it likes. A hard gate carries
no weight at all: failing it caps the match outright, which a large negative
weight would only approximate.

**3. Provenance is set from the record, not asserted by the model.** Whether
burden was read from a real Schedule of Assessments or inferred from phase and
design comes from `trial_info` — the rubric doesn't get to claim it read a
protocol that wasn't there. When step 3 flagged an SoA as `heading_only`, the
brief says so and asks the agent to treat it as weak evidence.

### The eligibility split, enforced

The brief presents population-defining and screening criteria in **separate
labelled sections**, and the agent must list every screening criterion it
declined to use in `excluded_screening`, with a reason. Requirements come from
"histologically confirmed ER+/PR+, HER2−, Stage II"; ECOG status and pregnancy
tests do not — they're checked per enrolled patient and say nothing about
whether a physician is a good site.

### Validation, and fix-and-resubmit

Output shape is guaranteed by structured outputs against a Pydantic schema
(`extra="forbid"`). Python then checks what a JSON schema can't express:

| Check | Why |
|---|---|
| 6–12 criteria | fewer is thin, more is unfocused |
| ≥1 criterion per dimension | a rubric missing `patient_population` isn't measuring fit |
| exactly 1–2 `hard_gate` | gates cap the whole match; they must stay rare |
| no duplicate criterion ids | ids are join keys for the scoring stage |
| `satisfies_if` ≥4 words | "has relevant experience" is not checkable |
| every criterion names ≥1 data source | otherwise nothing can verify it |

Failures go **back to the model with the specific problems** and it resubmits
(up to 3 attempts) — a mostly-good rubric gets fixed rather than discarded over
one bad criterion. Every attempt is recorded in the trace.

### `self_report` is a first-class answer

Each criterion names which of the five sources could verify it. When none can,
the honest answer is `self_report` rather than pointing at a source that only
weakly gestures at the question. `public_coverage` in the output reports what
fraction of the rubric public data can actually settle — the number that predicts
how often a truthful match lands at `insufficient_evidence` before an interview.

### Output

```
data/rubrics/{NCT}.json    rubric + scoring math + provenance + attempt trace
```

### Model and cost

**Default: `claude-sonnet-5`**, adaptive thinking, `effort: high`. Override with
`--model` or `TRIALFIT_MODEL` / `TRIALFIT_EFFORT`.

Measured cost per trial (~2.7k in, ~4k out including thinking):

| Model | $/trial | 19 trials | 34 pairs |
|---|---|---|---|
| `claude-sonnet-5` | $0.045 | **$0.86** | $1.54 |
| `claude-opus-4-8` | $0.114 | $2.16 | $3.86 |
| `claude-opus-4-7` | $0.114 | $2.16 | $3.86 |
| `claude-haiku-4-5` | $0.023 | $0.43 | $0.77 |

**Opus 4.7 and 4.8 are the same price** ($5/$25 per MTok) — stepping down a
version saves nothing. The saving comes from the Sonnet tier: Sonnet 5 is
$2/$10 at intro pricing (through 2026-08-31, $3/$15 after), 2.5× cheaper than
Opus at near-Opus quality on this kind of judgment work.

Every rubric records its own `trace.cost_usd`, the CLI prints a running total,
and `--budget 5.00` stops the run once that much has been spent.

## Step 5 — match, interview, precedent, report

```bash
# live (needs a key, ~30-90s/pair)
python scripts/run_match.py --nct NCT02513394 --npi 1336121789 --open
python scripts/run_match.py --prebuild --budget 3.00     # record every demo pair

# demo (no key, no network, instant)
python scripts/run_match.py --list
python scripts/run_match.py --replay --nct NCT02513394 --npi 1336121789 --open
```

Five stages: **rubric → adjudicate → precedent → interview → report.**

### The matcher has no tools

Its entire world is two artifacts already on disk — the rubric and the evidence
dossier. It cannot fetch, so it cannot introduce a fact mid-judgment that nothing
else in the pipeline has seen. Same division of labour as step 4: the model does
the semantic work (does this evidence meet this `satisfies_if`?), Python does the
arithmetic (gate, weighted score, coverage, recommendation).

Two guards make the output falsifiable rather than merely plausible:

- **Ref validation.** Every `evidence_refs` entry is checked against the refs the
  dossier actually contains. A rationale citing `PMID:99999999` when no such
  record was collected fails validation instead of shipping as a citation.
- **`unknown` is a real verdict.** The prompt draws the line between
  `not_satisfied` (evidence shows the criterion is *not* met) and `unknown`
  (nothing was collected either way), and forbids upgrading an absence into a
  charitable `partial`. Coverage below 50% forces `insufficient_evidence` no
  matter how well the answered criteria scored.

### The gap-closing interview

Public data answers *"is this a researcher"* well and *"does this person treat
THIS population"* badly. So the high-weight `patient_population` criteria come
back `unknown` and a genuinely good match lands at `insufficient_evidence`. That
verdict is correct — the fix is to ask the physician, then say plainly that the
answer came from them.

Measured on PALLAS × Erica Mayer with two criteria attested:

| | Before | After |
|---|---|---|
| Recommendation | possible fit | **strong fit** |
| Score | 7.5 / 14 | 13.5 / 14 |
| Coverage | 71% | 100% |

Self-report is never laundered: every attested record carries
`ref="attestation:<criterion_id>"`, a `[SELF-REPORTED]` summary prefix, and a
badge everywhere it appears in the report. A `not_satisfied` verdict is
deliberately **not** treated as a gap — re-asking would invite the subject to
overturn a finding. Answering `skip` leaves a criterion unresolved, which is
often the more honest demo.

### Precedent — what comparable trials did

No LLM, so it costs nothing. Finds terminal-status trials matching on condition +
phase + intervention type and tallies what happened to them. On PALLAS: **81
comparable trials, 83% completed, 17% stopped early, 6% specifically for failure
to enrol.**

That last number is the one a prospective site actually wants, and it comes from
classifying ClinicalTrials.gov's free-text `whyStopped` into accrual / efficacy /
safety / funding buckets.

**This is operational success, not efficacy.** CT.gov records whether a trial
finished and enrolled, not whether the treatment worked — a completed trial with
a negative result counts as completed. The caveat ships attached to the numbers,
and a sample under 30 trials is flagged as directional rather than statistical.

### The report

One **self-contained HTML file** — no CDN, no webfont, no image host (verified:
zero external resource requests). Opens from `file://`, survives being emailed,
and prints to PDF via the header button or Cmd/Ctrl-P. That beats generating a
PDF directly: no extra dependency, and the same artifact is both the on-screen
view and the download.

It carries the rubric it was scored against, every verdict with its rationale,
and **evidence refs rendered as links to the actual public record** — PubMed,
CT.gov, NPPES — so a reader can check any claim rather than trusting it. A score
without its rubric and its evidence is just an opinion.

### Trajectories — for the 3-minute demo

A live run makes 3–4 model calls and several API round trips. That's most of a
short demo spent watching a terminal.

Every run writes a **trajectory**: the finished artifacts plus the per-step
timings the live run actually took. `--replay` re-emits those steps from disk
with **zero API calls**, paced so the audience sees the pipeline progress
(`--pace 0.15` by default; `0` is instant, `1.0` is original speed).

What's replayed is the real run's output, not a mock — if the demo shows a score,
that score came from a model call that genuinely happened.

```
data/
├── match_reports/{NCT}__{NPI}.json            adjudication + scoring
│   └── ...__interviewed.json                  re-scored after the interview
├── precedent/{NCT}.json                       comparable-trial outcomes
├── reports/{NCT}__{NPI}.html                  the deliverable
├── trajectories/{NCT}__{NPI}.json             recorded run, for replay
└── demo_answers.json                          scripted interview answers
```

## Steps 6-8 — evaluation, patient proxy, UI

```bash
python scripts/serve_demo.py         # http://127.0.0.1:8765 — stdlib only
```

### Why evidence about the target trial is excluded

The dossier is built per *physician*, so for someone genuinely on a trial it
contains that trial. Left in, the question is circular: *"is this person a
plausible investigator for PALLAS?"* gets answered by *"they are an investigator
on PALLAS."*

It leaks through **two** routes, and both had to close:

| Route | Mayer × PALLAS |
|---|---|
| CT.gov investigator role | 1 record |
| PubMed **results papers** | **5 papers** |

The second is the subtle one — an investigator publishes the trial's outcomes,
so removing the role but leaving five papers titled *"…in the PALLAS randomized
trial"* is not an exclusion. Publications are filtered on **acronym or NCT id
only**, never drug or disease: "has published on palbociclib in early breast
cancer" is exactly the expertise the rubric should credit.

**In production this is a no-op.** A newly posted trial has no investigator
history and no results papers, so there is nothing to exclude. It only fires on
our retrospective set — which is the point: it makes the validation behave like
the deployment it's meant to predict. The control confirms it: a `weak` pair
loses 0 roles and 0 publications.

### Evaluation harness — three layers

**Tier separation (free).** Step 2 assigned every pair a tier from public signals
and nothing downstream reads it — a held-out label. If real scores rank the tiers
in order, the scorer tracks something real; if they overlap, it doesn't, and
every individual score is suspect. Reports mean/median per tier, monotonicity,
and AUC of `known+strong` vs `moderate+weak`.

**Perturbation (a few calls).** Delete one evidence source and re-adjudicate. If
the score doesn't move, the matcher is scoring the physician's name, not their
record.

**Integrity (free).** Recompute each stored score from its stored verdicts;
confirm one verdict per criterion and no phantom refs.

No LLM judge — most expensive layer, least informative.

### Patient-experience proxy

**No public dataset records whether trial participants were satisfied.** So this
composes what they *did*: voluntary withdrawal (40%), retention (35%), serious
AEs (15%), visit burden (10%). Simple weighted arithmetic, no fitting — a reader
can recompute it by hand from the components shown beside it.

Two honest corrections found in the data:

- **Treat-until-progression designs** record `COMPLETED = 0` because
  discontinuation *is* the endpoint. Scoring that as 0% retention punishes the
  design, not the experience — retention is marked uninterpretable and dropped
  from the composite. NALA went 34.7 → 56.9, EMBRACA 16.6 → 27.2.
- **Only trials that posted results can be scored**, which biases the cohort
  toward trials that finished. Reported, not corrected — correcting it would mean
  inventing numbers.

PALLAS scores 93.7 (99% retention, 0.6% voluntary withdrawal); its comparable
cohort medians 56.9 across a 27–89 range.

### The UI — replay and live

Two modes, and **live is a real run**, not a dressed-up replay:

| | Replay | Live |
|---|---|---|
| Rubric | loads from disk | **generates it if missing** |
| Match | loads | **adjudicates** |
| Precedent / patient | loads | **searches CT.gov and scores** |
| Interview | shows recorded Q&A | **asks, waits for you to type answers, re-scores** |
| Needs a key | no | yes |

The live interview is a genuine two-phase exchange: the run pauses at the
questions, because the answers have to come from a person. You type them in the
browser, hit re-score, and the matcher runs again on the augmented evidence —
then the report is rebuilt and a trajectory is written, so a live run becomes
replayable afterwards.


Physicians on the left; pick one and every trial in their bucket is listed with
its expected tier. Pick a trial and the stages stream in over SSE — trial
information, rubric, **the evidence the matcher may actually use**, the score
with every verdict and citation, comparable trials, the patient proxy, the
interview, the re-score. Each stage opens to show its own working.

Replay mode needs no key and no network. Live mode runs it for real.

## Scope is a parameter

`Scope` in [collect.py](trialfit/collect.py) holds the condition, the narrowing
term, the keyword tags, and the anchors. Breast HR+/HER2− is a preset in `SCOPES`,
not a hardcoded assumption — a new disease area is a new `Scope`, no other changes.

## Layout

```
trialfit/
├── ctgov.py       CT.gov v2 client — the only module that talks to that API
├── collect.py     scope presets, metadata extraction, scoring, bucketing
├── nppes.py       NPPES client + identity resolution (refuses to guess)
├── physicians.py  seed harvesting, roster building, demo selection
├── buckets.py     per-physician graded trial buckets + expected-fit tiers
├── pubmed.py      NCBI E-utilities publication search
├── cms.py         Open Payments + Medicare, with runtime dataset discovery
├── protocol.py    protocol PDF fetch, text extraction, visit-grid location
├── trialinfo.py   full trial detail + the eligibility split
├── evidence.py    per-physician dossier assembly across all five sources
├── rubric.py      evaluator agent — per-trial rubric, validation, scoring math
├── matcher.py     adjudicator — verdicts + ref validation; Python owns the score
├── interview.py   gap-closing interview — self-report evidence, re-score
├── precedent.py   comparable-trial outcomes from CT.gov (no LLM)
├── report.py      self-contained printable HTML report
├── pipeline.py    end-to-end run + trajectory record/replay
├── patient.py     patient-experience proxy from posted results (no LLM)
├── evaluate.py    tier separation · perturbation · integrity
├── server.py      stdlib demo server, SSE stage streaming
└── ui.html        the demo UI
scripts/
├── collect_trials.py       step-1 CLI
├── collect_physicians.py   step-2 CLI
├── collect_evidence.py     step-3 CLI
├── build_rubrics.py        step-4 CLI (needs ANTHROPIC_API_KEY)
├── run_match.py            step-5 CLI — live run, --prebuild, --replay
└── serve_demo.py           the demo UI
data/                       generated
```

## Caveats

- **Subtype tags are keyword heuristics.** Good for filtering; confirm borderline
  cases against the full record.
- **Setting tags read the title, conditions and summary only — never eligibility.**
  Eligibility text states what a trial *excludes* as often as what it studies, so
  matching "metastatic" there tagged 131 of 187 trials metastatic when only 8
  actually are. Adjuvant trials like `NCT02040857` were mislabelled.
- **PubMed author search is fuzzy.** `Mayer E[Author]` also matches other
  E. Mayers. The query is recorded next to the results so the ambiguity stays
  visible instead of being laundered into a clean count.
- **CT.gov investigator search is full text.** `confirmed` (listed in
  overallOfficials) is separated from `mentioned` (name appears somewhere in the
  record); the second is weak evidence.
- **Medicare underestimates practice volume** — Medicare claims only. A floor,
  never a total.
- **Expected-fit tiers are predictions, not ground truth.** Only `known` is
  factual (the physician is a listed official). The rest are heuristics to be
  validated against real scores, not trusted.
- **`official_names` needs entity resolution before it's useful.** Across all 187
  trials there are 176 listed officials but only **85 are real people** (82
  distinct) — the rest are corporate placeholders like "Novartis Pharmaceuticals"
  or "Clinical Trials". Filter these before seeding the physician side.
- **Eligibility is one free-text blob** mixing inclusion and exclusion. The
  manifest's counts are a richness proxy only.
- **Officials ≠ site PIs.** The record often lists a study chair or overall
  official rather than every site investigator.
