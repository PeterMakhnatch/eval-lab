# HAR-224: mimo-clean-v5 tasks with no cheat-ladder run, ranked, plus the next free census plan

Date: 2026-10-29. Cost: $0. **No task was executed**: this is a read-only analysis of committed files
by a stdlib script, `rank_gap.py` (no network, no Docker, no model calls).

Reproduce (repo root): `python research/experiments/mimo-v5-gap-rank-20261029/rank_gap.py --check`
(exit 0 = committed outputs reproduce byte-for-byte from the committed inputs; without `--check` it rewrites them).
Input SHA-256s and every number below are in `summary.json`.

| File | Content |
|---|---|
| `gap-ranking.csv` | all 2,355 gap tasks: 2,335 ranked (`rank`, `score`, five components, `reason`, `top_in_variants`), 20 held (`hold`, no rank) |
| `sample-plan.csv` | the 220-task census plan: wave A (100) and wave B (120) with stratum, rank and reason |
| `strata.csv` | wave B allocation per domain family x finding class x repo band (population, allocated, selected) |
| `summary.json` | seed, weights, input digests, counts, sensitivity table |

## 1. What "no v5 ladder run" means today

Population: `research/experiments/mimo-clean-v5/manifest.csv`, `status == built`: **2,666** tasks (32 skipped builds
are not packages). Manifest `domain` is `code` for every row, so "domain" below means language family.

A task has a **v5 ladder run** iff `mimo-clean-v5-census/results.csv` has a row with
`manifest_version == mimo-clean-v5` and `ladder_verdict in {clean, cracked}` (all are ladder 1.4.0).
Join keys: `task_id` for task level; `final_digest` against `digest-refresh.csv` (`old_digest`, `new_digest`) and the
current manifest digest for exact level. `unscored` rows count as no run.

| Step | Count |
|---|---|
| built v5 tasks | 2,666 |
| with a scored v5 ladder (311 clean, 0 cracked) | 311 |
| **gap: built tasks with no scored v5 ladder** | **2,355** |
| of which unscored-only (ladder started, no verdict) | 7 |
| held out of ranking (see below) | 20 |
| **eligible gap, ranked** | **2,335** |

Digest honesty. 306 of the 311 ladder-covered tasks are bound to the **pre-#848** digest (the other 5, 000776/000867/000919/000934/001901, to a different
v5-generation digest: the @5 node fold or an earlier build). **0 of 311 match the current manifest digest**: #848 changed 2,664 digests and
`digest-refresh.csv` marks all of them `unverified:package-changed`. So today *no* current v5 package has a ladder
run on its exact bytes; the 311 are "covered at the v5 generation", the 2,355 have no ladder at all in v5. The ranking
is of the 2,355. Re-laddering the 311 on current bytes is a separate question (not planned here).

Successors checked, none changes the gap: `mimo-clean-v6/manifest.csv` (same 2,698 rows on @10 digests; 0 ladder
clean/cracked rows bind to a v6 digest; `results.csv` has 15 control rows, all ladder `missing`);
`mimo-v6-full-fleet` (567 tasks: 5 `v6_verified`, controls only); `mimo-clean-v7` / `mimo-executed-clean-v1`
(repeat-3 reference/nop controls, no ladder); `mimo-clean-v5-admit` (88 tasks, 0 admit/reject: every gate stopped at
`oracle: infra`, so no ladder ran). Raw `census-rows.jsonl` (out of git, read-only look 2026-10-29) holds the same 311
clean tasks as `results.csv`. Pre-v5 ladders (v3 1.3.0, v4 1.4.0) exist for 235 / 95 eligible gap tasks and are used
only as a small score credit.

Reconciliation with the card's "1,729". The card quotes the v5 census table (`unverified:ladder-missing` 1,729, i.e.
tasks whose *first* missing check was the ladder). Parsing the pre-refresh label out of `verify-detail.csv` gives
1,677 such tasks after the 50-task local sample (README says 1,679; 2 unexplained rows) plus tasks whose first missing
check was something earlier. Full decomposition of the 2,355 by that label:

| label (first missing check, pre-refresh) | tasks |
|---|---|
| ladder-missing | 1,677 |
| probe-pending | 157 |
| nop-missing / cells-missing / oracle-missing | 128 / 127 / 31 |
| ladder-1.4.0-incomplete (v3 12/14 attacks only) | 115 |
| noexec | 73 |
| node-build-fold / node-build-leak | 13 / 1 |
| ladder-unscored / healthcheck / no-reward / timeout | 6 / 1 / 1 / 2 |
| env-broken, `fail:*`, baseline-unreliable, pre-existing-failures, @10-fold row | 11, 9, 1, 1, 1 |

(The README's "313 clean" differs from the 311 distinct tasks / 312 clean rows found by the join; 000919 has two rows.)

**Held, not ranked (20):** built rows whose manifest `reason` says `excluded-from-training`: env-broken 11, open-leak 6 (incl. 000928), `fail:unfair` 2, compile-only vacuous 1. They cannot join a clean set; listing is in
`gap-ranking.csv` (`hold`).

## 2. Ranking criteria and weights

Static evidence only. `score = 30*surface + 30*evidence + 20*leak_env + 10*leverage - 10*prior` (each component in
[0,1]; range -10..90). Ties break by `sha256(seed:task_id)`, so order is deterministic.

| Component | Source (committed) | Definition |
|---|---|---|
| `surface` (30) grader edit surface | `redteam-night/hack-scan-ledger.json` `plant_weight` (non-Python, 1,519 tasks) | weight/5: jest/mocha/vitest config surfaces 5, ava/Makefile 4, phpunit/rspec/karma 3, unclassified runner 2, Go TestMain/purge-covered 1. Python (no ledger row): fixed 0.2 (pytest; conftest/skip planting closed by purge + `separate-verifier`) |
| `evidence` (30) known weakness | `mimo-defect-map-v1/findings.json`, `predictions.json` (567 reference-bearing tasks only) | 0.7*min(n source-supported findings,3)/3 + 0.3*min(n non-universal predicted classes,3)/3 (`grader_tamperable`/`unstated_requirement` fire on all 567 and are ignored) |
| `leak_env` (20) leak / environment quirk | Python: `python-task-ledger/ledger.csv` `leak_channel` (fix released on PyPI .6, git only .4, pypi package .3) + `census_label` (grader_suspect .4, broken_environment .2). Non-Python: `code-harden-night/census.json` test-time downloads (unconditional .4, conditional .2). Both: +.4 for a triage note | triage note = pre-refresh label in {noexec, baseline-unreliable, pre-existing-failures, unscored, healthcheck, no-reward, timeout} or a non-final `verify-overrides.csv` row (the vacuous/unfair "retained-with-note" set); capped at 1 |
| `leverage` (10) repo exposure | `har81-mimo-sft/split.json` `split_group` (repo group; singleton = the task itself) | min(log2(eligible gap tasks in group),3)/3: a defect class found in one sibling likely repeats |
| `prior` (-10) earlier ladder credit | `mimo-clean-v4-census`, `mimo-clean-census` results | v4 ladder 1.4.0 clean 1.0; v3 ladder clean .5 (different grader bytes, so only partial credit) |

Weights are a judgment call, not fitted: there is no ground truth for "riskier" (0 cracks in 311 v5 ladders, 0 cracked in 101 v4 and 289 v3 ladder rows). The sensitivity table shows what depends on them:

| Variant | Spearman vs baseline (2,335 tasks) | Top-100 overlap |
|---|---|---|
| drop surface | 0.645 | 29 |
| drop evidence | 0.928 | 67 |
| drop leak_env | 0.814 | 47 |
| drop leverage | 0.982 | 86 |
| drop prior | 0.953 | 94 |
| equal weights 25/25/25/25, no prior | 0.926 | 68 |
| evidence x2 / surface x2 | 0.982 / 0.977 | 68 / 73 |

The order is stable overall (rho >= 0.81 except dropping `surface`) but the top-100 *membership* moves with the
weights; the dominant driver is `surface`. `top_in_variants` (0-8) in the CSV counts how many of the 8 variants keep a
task in its top 100: of the 100 top-ranked tasks 34 score 6, 13 score 7 and 1 scores 8 (a robust core), while 25 score <= 4.

### Top 25 (full list: `gap-ranking.csv`)

| rank | task | score | family | class | wave A | robust/8 | why |
|---|---|---|---|---|---|---|---|
| 1 | 001514 | 62.0 | js_ts | known-defect | yes | 8 | vitest config auto-load; 12 source-supported findings; predicted weak_check |
| 2 | 001808 | 54.0 | js_ts | known-defect | yes | 7 | mocha config/--require edits; 7 findings; instruction_grader_contradiction |
| 3 | 000603 | 50.0 | js_ts | known-defect | yes | 7 | vitest config auto-load; 2 findings |
| 4 | 002451 | 49.0 | python | known-defect | yes | 6 | 3 findings; env_mismatch_or_flaky, weak_check; leak/env 0.8 |
| 5 | 001516 | 48.3 | js_ts | known-defect | yes | 7 | vitest config; finding; predicted answer_reachable |
| 6 | 001387 | 48.0 | js_ts | grader-surface | yes | 7 | jest config/package.json key; repo group of 8 |
| 7 | 001576 | 45.0 | js_ts | known-defect | yes | 7 | jest config; finding; leak/env 0.4 |
| 8 | 000890 | 44.0 | js_ts | known-defect | yes | 6 | jest config; 2 findings |
| 9 | 000442 | 44.0 | go | known-defect | yes | 6 | 6 findings; predicted answer_reachable, weak_check |
| 10 | 000271 | 43.0 | js_ts | grader-surface | yes | 7 | jest config; weak_check; repo group of 8 |
| 11-25 | 001425, 002979, 001948, 001809, 001627, 000595, 000611, 000952, 000368, 000371, 002043, 002796*, 001927, 000882, 000788 | 39-42 | 8 js_ts / 7 python | mostly known-defect | all but *(sibling cap) | 5-7 | same mix |

## 3. Wave A: riskiest first (100 tasks)

Rank order, at most 3 tasks per repo group (siblings share a grader; a crack in a group should trigger its siblings,
not pre-spend them). Reaches rank 104 (4 siblings deferred). Composition: js_ts 75 / python 22 / go 2 / unknown 1;
class: grader-surface 60, known-defect 33, triage-note 6, leak-env 1. 45 have a reference fix. It is deliberately
js_ts-heavy: that is where the committed static evidence points. The low-surface families are covered by wave B.

## 4. Wave B: stratified sample of the rest (120 tasks)

Pool: eligible tasks whose repo group is not in wave A (2,223 tasks, 210 groups in the plan overall). Nested
stratification: domain family (5) -> static-finding class (5, first match wins: known-defect = any supported
finding; triage-note; grader-surface = non-Python `plant_weight >= 3`; leak-env = `leak_env >= 0.3`; none) ->
repo band by eligible group size (singleton / 2-4 / 5+). Cell allocation is proportional to sqrt(pool) with a
floor of 1 per non-empty cell (so small high-signal cells are oversampled), largest-remainder, within a cell
proportional across bands. One task per repo group. Seed `20261029`: candidates ordered by `sha256(seed:task_id)`.
Allocated 120, selected 120.

| domain | class | pool | n | singleton pop/n | pair-to-4 | five-plus |
|---|---|---|---|---|---|---|
| go | grader-surface | 6 | 2 | 4/1 | 2/1 | 0/0 |
| go | known-defect | 6 | 2 | 6/2 | 0/0 | 0/0 |
| go | leak-env | 20 | 4 | 18/4 | 2/0 | 0/0 |
| go | none | 625 | 16 | 523/13 | 97/3 | 5/0 |
| go | triage-note | 15 | 3 | 11/2 | 4/1 | 0/0 |
| js_ts | grader-surface | 209 | 10 | 209/10 | 0/0 | 0/0 |
| js_ts | known-defect | 15 | 3 | 15/3 | 0/0 | 0/0 |
| js_ts | leak-env | 48 | 5 | 48/5 | 0/0 | 0/0 |
| js_ts | none | 137 | 8 | 137/8 | 0/0 | 0/0 |
| js_ts | triage-note | 15 | 3 | 15/3 | 0/0 | 0/0 |
| other_typed | grader-surface | 28 | 4 | 28/4 | 0/0 | 0/0 |
| other_typed | leak-env | 27 | 4 | 27/4 | 0/0 | 0/0 |
| other_typed | none | 50 | 5 | 50/5 | 0/0 | 0/0 |
| python | known-defect | 59 | 6 | 57/6 | 2/0 | 0/0 |
| python | leak-env | 456 | 14 | 429/13 | 12/0 | 15/1 |
| python | none | 337 | 12 | 337/12 | 0/0 | 0/0 |
| python | triage-note | 50 | 5 | 50/5 | 0/0 | 0/0 |
| unknown | grader-surface | 4 | 2 | 4/2 | 0/0 | 0/0 |
| unknown | leak-env | 18 | 4 | 18/4 | 0/0 | 0/0 |
| unknown | none | 97 | 7 | 93/7 | 2/0 | 2/0 |
| unknown | triage-note | 1 | 1 | 1/1 | 0/0 | 0/0 |

Cost if later authorized and run locally (this PR runs nothing): ladder 1.4.0 at the measured local median of 15.5
min/task with 2 workers (the shared-daemon limit) = 12.9 h wall for wave A, 15.5 h for wave B, $0 provider spend.
Run order: wave A first, stop on any crack and expand to that repo group's siblings, then wave B. A clean wave B
(0/120) bounds the crack rate of the sampled pool below 2.5% at 95% (`1-0.05^(1/120)`); the allocation is not
proportional, so read that as indicative, not as a population estimator (reweight by stratum for one).

## 5. Limits

- Static, unverified proxies. Nothing here shows a task is crackable; 0 of 311 laddered v5 tasks were. Ranks say
  where to look first, not where cracks are.
- `evidence` exists only for the 567 reference-bearing tasks, which were also the most scrutinised: reference-bearing
  tasks are favoured by construction (only 472 of the 2,335 eligible tasks are in the 567-task defect map, and 113 of those have a supported finding; the other 1,863 cannot score on `evidence`).
  Findings are keyed by task id on historical packages; v7 repaired children have other digests, so a finding may no
  longer apply to the v5 bytes that the ladder would run on.
- `surface` is a hand-annotated runner class from one overnight scan (V-scores are near-uniform, 29-41). Python
  tasks get a constant, so Python rank is driven by findings and leaks only. 365 tasks are "unclassified runner".
- Weights are judgment; see the sensitivity table. `leverage` and `prior` are small by design.
- Ladder 1.4.0 attacks are model-free and never found a crack; a clean ladder is weak evidence (the redteam-night
  notes record manual wins the ladder misses). The plan buys coverage, not proof of cleanliness.
- The 311 existing v5 ladders are on pre-#848 bytes and are not re-ranked; wave A/B runs would be on current digests.
- Published job directories with "cheat" in the name (one-off listing: 78 dirs naming 21 task ids, mostly HAR-202/v1-v4
  chain and HAR-208 single-attack jobs) were not joined: they predate v5 or run single attacks.
- Wall-time uses the 50-task local median (1.0-35.4 min spread) and emulated amd64; timeouts (2 strikes) are possible.
  10 eligible tasks carry `retry_flag` (earlier unscored/timeout), flagged in the CSV, not excluded.
