# mimo-clean-v2: canonical clean set for the MiMo code pool (all languages)

Date: 2026-10-09. $0 paid compute (local Docker + local derivation only;
no model calls, no Modal/Daytona).

## What this is

One clean package per code task, each carrying the canonical chain in its
lineage:

```
Python (1,180 ledger rows): ledger run package (repairs)
  -> strip-future-history@1 -> purge-installed-copies@1 (scoped)
  -> purge-build-caches@2 -> mtime-normalize@1 -> separate-verifier@3
  (+ reference solution where a fix exists)

Non-Python (1,518 snapshot tasks): snapshot task dir
  -> strip-future-history@1 -> purge-build-caches@2
  -> mtime-normalize@1 -> separate-verifier@3
  (purge-installed-copies is a Python pip mechanism: noted, not derived)
```

- Builder: `src/evallab/mimo_clean.py`; CLI: `evallab mimo-clean build|verify-local`
- Design doc: `docs/mimo/clean-set.md`
- Manifest: `manifest.csv` in this directory
  (2,698 rows: 2,666 built, 32 skipped discards)
- Predecessor: `research/experiments/mimo-clean-v1/` (kept as history)

## Build result (2026-10-09, `evallab mimo-clean build --workers 8`)

Reference fixes resolved from the HAR-191 `oracle_sweep.csv`
(`mimo-reference-fixes/index.csv` was not on main at build time; see below).

| Status | Count | Detail |
|---|---|---|
| built | 2666 | 1,148 Python keep/fix rows + 1,518 non-Python snapshot tasks |
| skipped | 32 | all `discard` verdicts, reason cites the ledger row |

Chain shapes over the 2,666 built:

| Chain | Tasks |
|---|---|
| `strip-future-history@1>purge-build-caches@2>mtime-normalize@1>separate-verifier@3` | 2664 (1,146 Python + 1,518 non-Python) |
| `strip-future-history@1>purge-installed-copies@1>purge-build-caches@2>mtime-normalize@1>separate-verifier@3` | 2 (`format-code-task-001269`, `format-code-task-002308`: purge carried in their repair run packages) |

Per-domain counts (manifest `language`):

| Language | Rows | Built | Skipped |
|---|---|---|---|
| python | 1180 | 1148 | 32 (discard) |
| go | 721 | 721 | 0 |
| javascript | 388 | 388 | 0 |
| typescript | 166 | 166 | 0 |
| unknown | 130 | 130 | 0 |
| ruby | 25 | 25 | 0 |
| php | 23 | 23 | 0 |
| java | 22 | 22 | 0 |
| c++ | 13 | 13 | 0 |
| c | 7 | 7 | 0 |
| rust | 7 | 7 | 0 |
| scala | 6 | 6 | 0 |
| kotlin | 3 | 3 | 0 |
| dart | 3 | 3 | 0 |
| lua | 1 | 1 | 0 |
| elixir | 1 | 1 | 0 |
| swift | 1 | 1 | 0 |
| svelte | 1 | 1 | 0 |

Purge legs over the 1,148 built Python tasks: 5 fail-closed with per-task
reasons (002552 poetry py3.8, 000792/002486 no project name, 002139
bitbake, 002391 PEP 668), 2 carried (CONFIRMED_PURGE), 1,141 scope-skips
per the HAR-194 CONFIRMED_PURGE-only stance (see `docs/mimo/clean-set.md`).
All 1,518 non-Python tasks note purge-installed-copies as n/a.

- 192/1148 built Python tasks carry an oracle-pass reference fix
  (`solution/solve.sh` built from the HAR-191 patch; manifest
  `reference_fix`). No non-Python task ships a reference fix with the
  dataset (`reference_fix=none`).
- Ledger membership always wins over the snapshot category: 002361 is
  snapshot-category Go but builds through the Python chain from its ledger
  run package (it is the 1,180th ledger row, hence 1,180 + 1,518 = 2,698
  manifest rows).
- Every row ships `verify=unverified`: the fleet census grades the
  packages later (see the design doc).

## Reproducibility proof

The builder is deterministic and idempotent (content-addressed derives;
existing `(task, transform, parent)` records reused, never duplicated).
After deleting all 7,998 newly derived lineage records, a from-scratch
rebuild reproduced the manifest exactly:

- **2698/2698 rows identical**: `final_digest`, `status`, and `chain`
  all match (receipt: `/tmp/mimo-clean-v2-first.csv`, scratch, not
  committed; the committed manifest is the rebuilt copy).
- The from-scratch run exercised the orphan-recovery path (packages
  present in the shared store without records are moved aside,
  re-derived, reconciled by digest, and dropped on match) with zero
  mismatches.

## Local acceptance (2026-10-09, `evallab mimo-clean verify-local`)

Per task on local Docker (`--network none` via each task's declared
policy), pinned `xiaomimimo/mimo-v2.6-rl-oss` images (pulled on demand):
oracle control (when a reference fix exists), nop control, and the full
12-attack cheat ladder v1.2.0 in-trial. Acceptance = **oracle 1, nop 0,
cheat clean**. Jobs:
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-09/mimo-clean-v2/`
(raw, out of git). 12 tasks: 6 Python (incl. the ADDOPTS-clearing 000666
and the unittest 000803) and 6 non-Python across go/jest/mocha/vitest/
usecase-sh:

| task | purge leg | oracle | nop | cheat (cracked/trials) | verdict |
|---|---|---|---|---|---|
| format-code-task-000666 (pytest, ADDOPTS-cleared) | scope-skip | 1 | 0 | 0/1 | PASS |
| format-code-task-000803 (unittest) | scope-skip | 1 | 0 | 0/1 | PASS |
| format-code-task-002552 (poetry py3.8) | fail-closed | 1 | 0 | 0/1 | PASS |
| format-code-task-001269 | carried (CONFIRMED_PURGE) | n/a (no oracle-pass patch) | 0 | 0/1 | PASS |
| format-code-task-001809 | scope-skip | 1 | 0 | 0/1 | PASS |
| format-code-task-002391 (PEP 668) | fail-closed | 1 | 0 | 0/1 | PASS |
| format-code-task-000553 (go-test) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |
| format-code-task-000045 (mocha) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |
| format-code-task-000236 (jest) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |
| format-code-task-000128 (vitest) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |
| format-code-task-000291 (usecase-sh) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |
| format-code-task-002928 (mocha) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |

**Acceptance: 12/12 pass.**

Notes:

- Non-Python tasks ship no reference fix with the dataset, so their
  oracle cell is n/a; acceptance is nop 0 + cheat clean. Per-task oracle
  behavior under `@3` for these runners was proven separately by the
  `@3` port validation (history-extracted or debugged oracles;
  see `docs/mimo/separate-verifier.md`).
- 000047 (`lib/` build-output leak, OPEN pending `purge-build-caches@3`)
  was deliberately left out of the acceptance set; the manifest `chain`
  shows every package carries `@2` until the rebuild.

## Reference-fix source note

`research/experiments/mimo-reference-fixes/index.csv` (ReferenceSweep
lane) was not on main when the fleet built, after polling `origin/main`
through the build window. The build used `oracle_sweep.csv` (192
oracle-pass fixes, identical coverage to v1). Rebuilding with
`--reference-index` once the index lands re-derives only the `@3` step
for tasks that gain a fix (the `solution` lineage input changes);
everything else is reused by digest.

## Spend

$0.00 — no paid compute used (no `evallab spend day` needed; local Docker
and local derivation only).
