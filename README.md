# trial_fit

Matching physicians to clinical trials as potential **site investigators**, using
only public data, with every claim traceable to a source record.

Built step by step. This is where we are:

- [x] **Step 1 — sample trial set** (`scripts/collect_trials.py`)
- [ ] Step 2 — trial agent → RequirementProfile
- [ ] Step 3 — physician agent → EvidenceDossier
- [ ] Step 4 — matcher → MatchReport
- [ ] Step 5 — gap-closing interview
- [ ] Step 6 — evaluation harness
- [ ] Step 7 — orchestrator + demo UI

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
| **broad** | 56 | Breadth, for mining historical investigators. |
| other | 73 | Off-target; ignored downstream. |

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

## Scope is a parameter

`Scope` in [collect.py](trialfit/collect.py) holds the condition, the narrowing
term, the keyword tags, and the anchors. Breast HR+/HER2− is a preset in `SCOPES`,
not a hardcoded assumption — a new disease area is a new `Scope`, no other changes.

## Layout

```
trialfit/
├── ctgov.py       CT.gov v2 client — the only module that talks to the API
└── collect.py     scope presets, metadata extraction, scoring, bucketing
scripts/
└── collect_trials.py   step-1 CLI
data/                   generated
```

## Caveats

- **Subtype tags are keyword heuristics.** Good for filtering; confirm borderline
  cases against the full record.
- **`official_names` needs entity resolution before it's useful.** Across all 187
  trials there are 176 listed officials but only **85 are real people** (82
  distinct) — the rest are corporate placeholders like "Novartis Pharmaceuticals"
  or "Clinical Trials". Filter these before seeding the physician side.
- **Eligibility is one free-text blob** mixing inclusion and exclusion. The
  manifest's counts are a richness proxy only.
- **Officials ≠ site PIs.** The record often lists a study chair or overall
  official rather than every site investigator.
