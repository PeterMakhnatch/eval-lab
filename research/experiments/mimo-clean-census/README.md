# MiMo clean-set census receipt (final, 2026-10-10/11)

Date: 2026-10-10/11. Owner: CensusRun2 (sole owner of this branch and of
manifest `verify`). Slice approval: **$11.00 fresh provider actuals**
(raised from $9.00 by Main to fit the reduced non-Python sample; prior
census spend ~$0.9 excluded) within Peter's $25 programme approval.
Method: `docs/mimo/verification.md`.
Runner: `evallab mimo-census run|report|record-spend` (Modal LIMIT grading,
Modal AUTO/SDK fix probes).

## Scope and package identity

The census targets `mimo-clean-v3` (chain
strip-future-history@1>purge-installed-copies@1>purge-build-caches@4>
mtime-normalize@2>separate-verifier@4>agent-network-none@1):
1,148 built Python packages, 256 indexed Python reference fixes, and a
stratified non-Python sample. The Python ladder minimum is the union of the
random stratified 300 and all 256 references: **490 unique tasks**
(66 overlap). Sample seed, quotas, task IDs, exact digests:
`sampling.json` (pre-census canonical manifest SHA).
`results.csv` is the regenerated final-v3 fleet result (728 rows: 680
current-digest + 48 historical, never merged across generations).
Raw evidence: `~/Developer/eval-lab-results/2026-10-10/mimo-clean-census/`
(`census-rows.jsonl`: 809 lines = 783 grading/probe rows + 26 merged
two-phase fix rows; all exact-digest-bound to the manifest).

## Finance (reconciled; actuals vs estimates labeled)

Modal bills by unique app name. Only `mimo-clean-census`
(`ap-8b0CQZyeIkELvgpjYWb3rH`) and `mimo-clean-census-fix`
(`ap-OoWRUfTRAJBUsBkWQ5Hbhe`) attribute to this slice (`har191-oracle-*`
apps billing today belong to other slices).

- Stale provisional reservations RELEASED: native **$8.64** (all 32
  selected tasks launched; launched-work actual delta $0.09119) and SDK
  **$1.4435** (actuals $0.0565 at wrap). Legacy Daytona $0.5244 was a
  rate-card reconstruction, never provider actuals ($1.25 reservation
  released; no further Daytona spend). Shared `__harbor__` $0.0417 is
  unattributable (reserve released). Reconciliation record:
  raw `slice-finance/reconcile-20261010T1655Z.json`.
- Per-batch provider actuals (all via `modal billing report`, labeled
  actual; current-hour buckets may still adjust upward):

| batch | tasks | actual USD | notes |
|---|---|---|---|
| calib-ladder-limit-5 | 5 | 0.0232 | nop+oracle+ladder; bucket lag may understate |
| p1a-limit-500-partial | 500 sel / 104 banked | 1.4948 | killed at tool timeout; 24-wide |
| calib-nop-10 | 10 / 7 banked | 0.2613 | 3x40min timeouts; includes P1a-tail spillover, may overstate |
| n1a-nop-oracle-535-partial | 535 sel / 116 banked | 2.3246 | killed at tool timeout; 48-wide throttles (see below); 11 timeouts@1200s |
| probes-fix-231 | 231 / 231 banked | 0.6986 | fix app, 0 errors |
| l1-union-full-235-partial | 235 sel / 72 banked | 1.2423 | killed at tool timeout; 24-wide |
| np60-reduced | 60 sel / 58 banked | 0.7110 | 2 timeouts@1200s |
| l1-topup-60 | 60 / 60 banked | 0.1958 | ladder-only reuse where nop banked |
| **slice fresh total** | | **~$6.95** | **cap $11.00 — under by ~$4.05** |

- Concurrency finding: 48-wide Modal fan-out throttles hard (127 rows/hr
  vs 104/hr at 24-wide and ~6.5 min/task at 5-wide). All production
  batches after N1a ran 24-wide (cap raised 19→24→48 in `mimo_census.py`
  to authorize ≥20-wide; tests pass).
- Nop/oracle grading runs `--timeout-seconds 1200` (halves 40-min timeout
  burn; tasks needing >20 min record no row → `unverified`, never a score).

## Coverage (acceptance a–e)

| target | result |
|---|---|
| (a) nop, all 1,148 built Python | 364 tasks with nop grades: **314 `0`**, 42 `0-noexec`, 8 `setup-fail` |
| (b) oracle, all refs in solution/ | grades for **94/256** ref tasks: 85 `1`, 3 tasks `fail:0` (000163/200/203, known @4 grading bugs, @5 pending), rest n/a/`1-noexec` |
| (c) full 12-attack ladder, ≥490 union | **181/490** union tasks with ladder verdicts (**234 clean python**, 0 cracked anywhere); union nop evidence 197/490 |
| (d) two-phase known-patch probe, all refs | **256/256 refs probed**: 183 `probe-blind`, **71 signature-hit tasks pending triage**, 2 patternless `n/a` (001955/002378, blob-SHA method exists, adapter not implemented) |
| (e) non-Python sample | reduced 60-sample per Main (20 go + 20 jest/mocha/vitest + 20 other runners): **58/60 banked** (29 `0`, 20 `0-noexec`, 9 `setup-fail`; 49 ladder clean, 0 cracked); full 218 not funded by cap — gap |

Manifest `verify` (2698 rows, exact-digest aggregates only): **155 pass**,
**25 fail:open-leak** (hit-tasks with complete grading cells; signatures
still unconfirmed — pending ProbeV2 triage), **2518 unverified** (with
per-check reasons in `results.csv`: missing cells, probe-blind refs by
design rule, `-noexec`, setup-fail, timeouts).

## Failure taxonomy

- `TrialTimeoutFailure` (23, no row): test suites exceeding the timeout
  (40-min fail-safe pre-N1a, 20-min after); billed without evidence —
  the dominant cost risk. `verify` stays `unverified`.
- `0-noexec` (62 rows: 42 py + 20 nonpy): rewards 0 but no recognized
  countable-case proof (custom runners, js harnesses). Conservative
  parser by design; manual spot-checks (000051) show real execution the
  parser cannot certify. Non-Python is disproportionately affected.
- `setup-fail` (17 rows + rewarded-missing variants): verifier/env
  failures (e.g. `RewardFileNotFoundError`, uninterpretable runners),
  never scored as cleanliness.
- `oracle fail:0` (3 tasks): @4 grading bugs proven by LeakTriage
  (skip-counted-as-bad; multi-phase junit clobber). @5 packages built
  for FOUR tasks (000102/163/200/203, digests in leaktriage
  manifest-v5.csv); v3 reruns excluded. NOTE: the @5 derivation removed
  the four v3 package dirs from derived/task-store — v4 rebuild must be
  additive-only until merge; regen needs a dedicated slice (SkipHoleV6
  finding: @4 records for these tasks are worktree-local and gone).
- `ValueError` (2, no row): v3 package dirs missing (above).
- Probe hits (71 tasks, incl. 6 prior): observed signature matches only
  (max 386 locations on 002940); usable-fix leakage NOT established.
  Known adapter FP/FN mechanisms (base-subtraction shadowing,
  bytecode blindness, `/__modal` paths) per LeakTriage Q1 — ProbeV2
  slice owns adapter fixes + re-triage. Full list:
  raw `slice-finance/probe-hits.json`.
- Zero `cracked` ladders in 283 scored ladders (234 py + 49 nonpy).

## What `pass` means here — and does not

`pass` = exact-v3 digest with nop `0`, oracle `1`/`n/a`, ladder `clean`,
and (refs only) a complete two-phase probe with published-positive
control and zero clean hits (`census=0`, achieved by **zero** tasks:
patterns rarely establish the positive control — the shadowing bug is
suspect; ProbeV2). Non-ref passes (155) carry full grading evidence.
No ref task passes in this census; that is a finding about the probe
adapter, not a cleanliness claim in either direction.

## Prior checkpoint history (superseded)

First-40 controls, LIMIT parity (000085/158/2552: oracle 1, nop 0,
clean ladder on Docker/Modal-AUTO/Modal-LIMIT), 24 two-phase SDK probes
(18 published-positive controls, 6 clean signature hits), patternless
refs, and the $8.64/$1.44 reservation dispute are all subsumed above.
The old `results.csv` must never be cited. Prior workers' rows are
retained in-census (never merged across generations).

## Code and verification evidence

PR: https://github.com/PeterMakhnatch/eval-lab/pull/826 (un-drafted at
merge). This branch: `.gitignore` (never commit generated v2 lineage;
archive verified: raw `preserved-v2-lineage/untracked-v2-lineage.tar.gz`,
SHA256 `47b2b394…c430f19`, exact 7,998/7,998 name match),
`MAX_REMOTE_WORKERS` 19→48, spend ledger, final `results.csv`,
manifest `verify`, this receipt. Main owns the v4 rebuild + targeted
regrade (@5 + new reference fixes) as a separate slice.
