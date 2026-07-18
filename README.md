# trial_fit

Matching physicians to clinical trials as potential **site investigators**, using
only public data, with every claim traceable to a source record.

Built step by step. This is where we are:

- [x] **Step 1 — trial pool** (`scripts/collect_trials.py`)
- [x] **Step 2 — physicians + their trial buckets** (`scripts/collect_physicians.py`)
- [ ] Step 3 — trial agent → RequirementProfile
- [ ] Step 4 — physician agent → EvidenceDossier
- [ ] Step 5 — matcher → MatchReport
- [ ] Step 6 — gap-closing interview
- [ ] Step 7 — evaluation harness
- [ ] Step 8 — orchestrator + demo UI

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
└── buckets.py     per-physician graded trial buckets + expected-fit tiers
scripts/
├── collect_trials.py       step-1 CLI
└── collect_physicians.py   step-2 CLI
data/                       generated
```

## Caveats

- **Subtype tags are keyword heuristics.** Good for filtering; confirm borderline
  cases against the full record.
- **Setting tags read the title, conditions and summary only — never eligibility.**
  Eligibility text states what a trial *excludes* as often as what it studies, so
  matching "metastatic" there tagged 131 of 187 trials metastatic when only 8
  actually are. Adjuvant trials like `NCT02040857` were mislabelled.
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
