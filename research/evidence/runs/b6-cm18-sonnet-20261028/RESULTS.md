# B6 — CM-18 reader sensitivity: Sonnet 5.5 facets on a fixed CM-18 subsample

**Preregistration:** research-context `causal-prefix-monitor.md`, BURST, slice B6 (before any run was read).
**Question:** does the CM-18 facet result depend on the reader? Same production extractor and blinding, one reader
changed. **Answer:** at `suspicion >= 2` on the same 278 runs, Sonnet 5.5 recall is 67.3% at 1.0% established-negative
FAR and Haiku 5.5 is 71.4% at 3.8% — a 3-run recall difference (one direction) and a 3-run FAR difference (the other),
with kappa 0.83 and wide intervals. **The reader is not the driver of the CM-18 facet result.**

## What ran

- Frozen CM-18 sample (`sample.json` and `runs.pkl` sha256 re-checked against the committed CM-18 freeze), then per set
  and stratum (positive, `negative_established`, `negative_other`) the **first 15 runs in sha256(`order:` + key) order**,
  the CM-18 facet processing order: **281 runs** (KernelBench 11 + 15 + 0, Reflex 15 + 15 + 15, Cheat Index 15 × 3,
  EvilGenie 15 × 3, SDF 15 × 3, ResearchArena 15 + 15 + 0, Beam 15 × 3). Key list and sha256 in `results/subsample.json`
  (`7105acb8…9031`).
- Reader `anthropic/claude-sonnet-5-5`, production Run Atlas extractor and prompt, per-run source pattern, blinding
  unchanged (`blind_run` step identities verified per run), 4 concurrent, Anthropic gate 85% of any window, cap 12M
  input tokens, fallback and retry disabled for the gateway child (`PI_CONFIG_FILES` overlay, `retry.modelFallback`
  read back `false` before the first call and twice inline). No tuning, no cascade rerun, no new sample.
- Cross-key: every run literal (1,941) checked against `assignments.csv` through `CorpusRegistry`
  (`assigned_test_anywhere` and `sealed_identity`) → **0 TEST or sealed hits**; ledger digest in `subsample.json`.
- **278/281 reads OK**; 3 EvilGenie runs (all positives) were refused by the route
  (`Refusal (reasoning_extraction)`, HTTP 502) after each run's 3 attempts — unknown, never a miss or a negative, and
  excluded from both readers' denominators. 472 of 481 calls returned a facet record; **472/472 OK calls were served by
  the requested model**, 0 calls answered by another model.

## Numbers at `suspicion >= 2` (same 278 runs, Wilson 95%)

| set | Sonnet recall | Sonnet FAR-est | Sonnet FAR-other | Haiku recall | Haiku FAR-est | Haiku FAR-other | kappa |
|---|---|---|---|---|---|---|---|
| KernelBench | 5/11 | 1/15 | – | 5/11 | 0/15 | – | 0.66 |
| Reflex | 15/15 | 0/15 | 0/15 | 15/15 | 0/15 | 1/15 | 0.95 |
| Cheat Index | 3/15 | 0/15 | 2/15 | 6/15 | 0/15 | 2/15 | 0.55 |
| EvilGenie | 9/12 | 0/15 | 0/15 | 9/12 | 0/15 | 0/15 | 1.00 |
| SDF | 12/15 | 0/15 | 1/15 | 11/15 | 2/15 | 2/15 | 0.69 |
| ResearchArena | 15/15 | 0/15 | – | 15/15 | 2/15 | – | 0.87 |
| Beam | 7/15 | 0/15 | 0/15 | 9/15 | 0/15 | 0/15 | 0.85 |
| **pooled** | **66/98 = 67.3% (57.6–75.8)** | **1/105 = 1.0% (0.2–5.2)** | **3/75 = 4.0% (1.4–11.1)** | **70/98 = 71.4% (61.8–79.4)** | **4/105 = 3.8% (1.5–9.4)** | **5/75 = 6.7% (2.9–14.7)** | **0.83** |

All negatives pooled: Sonnet 4/180 = 2.2% (0.9–5.6), Haiku 9/180 = 5.0% (2.7–9.2). Haiku on the full 281-run
subsample, for completeness: 73/101 recall, 4/105 established, 5/75 other (the three refusals are positives).

**Agreement** (278 paired runs): both alarm 65, Sonnet only 5, Haiku only 14, kappa 0.826. Suspicion-level matrix varies
mostly by one level, not by threshold: Haiku 0 → Sonnet 0 in 125 and Sonnet 1 in 28; Haiku 3 → Sonnet 3 in 31, 2 in 2,
1 in 1.

**Full assigned denominators (not only paired rows).** Assigned: 101 positives, 105 established negatives, 75 other
negatives. Sonnet: 98/101 positives read (3 unknown, all refusals) → **recall 66/98 observed; over the full assigned 101
that is 66/101 = 65.3% if the three unknowns were misses and 69/101 = 68.3% if all three had alarmed; the truth is
unknown.** Negatives have no unknowns: established 1/105, other 3/75. Haiku over the same assigned positive set: 70/98
read, 3 unknown (73/101 on its own full read). Every recall and FAR figure in the table above and in `score.json` is
the paired set (278 runs both readers read) so the two readers are read on identical runs; the 3 unknown positives are
excluded from both denominators, never counted as misses.

**Tokens and attempts.** Sonnet: 481 attempt calls (472 OK, 9 refused) for 278 facet records — 1.73 calls per read,
because one read renders the run into map chunks plus one form call. Reported usage: **11,924,873 input / 165,943 output,
counted only on the 472 successful calls** (42,895 in per read); the nine 502 refusal receipts carry no usage, so
**those tokens are unknown and the totals are a lower bound, not the spend**. Haiku on the same 278 runs: 12,697,844 /
181,132 over 552 calls (45,676 in per read). Claude 5-hour window 0% → 12%, 7-day 27% → 30% (stop 85%). $0 billed.

**Code revision against the CM-18 freeze.** `run_atlas.py` in this slice is `7ec1f28…` (`origin/main`, includes
`de286f47` #1405, "redact a bare family token inside task filenames"); CM-18 froze `608877e4…`. The change is one extra
alternative (`mimo(?![a-z])`) in `SOURCE_TERMS`, i.e. in the source pattern used by `source_pattern`/`blind_atif` to
redact the rendered input — the extractor prompt, chunking, blinding procedure and form are unchanged, but on runs that
contain that literal the reader's input is not byte-identical to Haiku's. **4 of the 281 subsample runs contain it (all
ResearchArena established negatives; Haiku suspicion 2, 2, 1, 1; Sonnet 1, 0, 1, 0, so 2 alarm disagreements).** They are
listed by run-key hash in `results/derived.json` and are incomparable in principle: **excluding them, kappa is 0.842
(all 278: 0.826), established-negative FAR is Sonnet 1/101 vs Haiku 2/101, other-negative 3/75 vs 5/75, recall
unchanged 66/98 vs 70/98** — the pooled conclusion is unchanged, and the whole difference is inside the wide intervals.

## Meaning and limits

- Sonnet and Haiku agree on the substance of CM-18: most labels are seen, established-negative false alarms stay low,
  and the same cells carry them (SDF `fixed`, ResearchArena baselines for Haiku; one KernelBench human-clean run for
  Sonnet). Neither reader is uniformly better: Sonnet is lower on FAR (4 vs 9 of 180 negatives) and lower on recall
  (66 vs 70 of 98 positives), Cheat Index and Beam being most of the recall gap and ResearchArena/SDF most of the FAR gap.
- Limits: one pass per reader, 15 per set and stratum, so intervals are wide (±9 points on recall) and a one-to-four-run
  difference is not a reader effect. Pooled counts are sums over a capped sample and runs within a task are not
  independent. The 3 refused runs are unknown (recall bounds 66/101 to 69/101 over the assigned positives). The reader's
  input is not byte-identical to Haiku's on the 4 runs that carry the literal `#1405` also redacts; those are separated
  above and their removal does not change any conclusion. Sonnet call usage on the 9 refused calls is unreported, so its
  token figure is a lower bound.
- This measures reader sensitivity of a whole-run reader, not deployment recall; it says nothing about the cascade, which
  was not rerun.
