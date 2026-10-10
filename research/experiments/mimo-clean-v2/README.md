# mimo-clean-v2: canonical clean set for the MiMo code pool (all languages)

Date: 2026-10-09/10. $0 paid compute (local Docker + local derivation only;
no model calls, no Modal/Daytona).

## What this is

One clean package per code task, each carrying the canonical chain in its
lineage:

```
Python (1,180 ledger rows): ledger run package (repairs)
  -> strip-future-history@1 -> purge-installed-copies@1 (scoped)
  -> purge-build-caches@3 -> mtime-normalize@2 -> separate-verifier@3
  (+ reference solution where a fix exists)

Non-Python (1,518 snapshot tasks): snapshot task dir
  -> strip-future-history@1 -> purge-build-caches@3
  -> mtime-normalize@2 -> separate-verifier@3
  (purge-installed-copies is a Python pip mechanism: noted, not derived)
```

- Builder: `src/evallab/mimo_clean.py`; CLI: `evallab mimo-clean build|verify-local`
- Design doc: `docs/mimo/clean-set.md`
- Manifest: `manifest.csv` in this directory
  (2,698 rows: 2,666 built, 32 skipped discards)
- Predecessor: `research/experiments/mimo-clean-v1/` (kept as history)

`purge-build-caches@3` (vals-routes-v3, PR #810) and `mtime-normalize@2`
(mtime lane, PR #813) both landed during this slice; the builder resolves
the active generation at import, so the shipped fleet carries both (see
chains). Ledger membership always wins over the snapshot category: 002361
is snapshot-category Go but builds through the Python chain from its
ledger run package (it is the 1,180th ledger row, hence 1,180 + 1,518 =
2,698 manifest rows).

## Build result (`evallab mimo-clean build --workers 8`)

Reference fixes resolved from the HAR-191 `oracle_sweep.csv`
(`mimo-reference-fixes/index.csv` was not on main through the whole build
window; the builder prefers the index when present — see below).

| Status | Count | Detail |
|---|---|---|
| built | 2666 | 1,148 Python keep/fix rows + 1,518 non-Python snapshot tasks |
| skipped | 32 | all `discard` verdicts, reason cites the ledger row |

Chain shapes over the 2,666 built:

| Chain | Tasks |
|---|---|
| `strip-future-history@1>purge-build-caches@3>mtime-normalize@2>separate-verifier@3` | 2664 (1,146 Python + 1,518 non-Python) |
| `strip-future-history@1>purge-installed-copies@1>purge-build-caches@3>mtime-normalize@2>separate-verifier@3` | 2 (`format-code-task-001269`, `format-code-task-002308`: purge carried in their repair run packages) |

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
- Every row ships `verify=unverified`: the fleet census grades the
  packages later (see the design doc). In particular, `built` means the
  chain derived — it does NOT mean the package sets up: the census's nop
  sweep owns enumerating setup-failures (first known case below).

## Reproducibility proof

The builder is deterministic and idempotent (content-addressed derives;
existing `(task, transform, parent)` records reused, never duplicated).
The `@2`-generation fleet was proven by wipe-and-rebuild: after deleting
all 7,998 newly derived lineage records, a from-scratch rebuild
reproduced the manifest exactly — **2698/2698 rows identical**
(`final_digest`, `status`, `chain`; first copy at
`/tmp/mimo-clean-v2-first.csv`, scratch, not committed). The orphan
recovery path (packages present without records are moved aside,
re-derived, reconciled by digest, dropped on match) ran with zero
mismatches. After the `@3`/`mtime@2` adoptions, a second wipe-and-rebuild
on the shipped generation reproduced the committed manifest exactly again
(**2698/2698 rows identical**; copy at `/tmp/mimo-clean-v2-shipped.csv`,
scratch, not committed) — including across the `ruff format` pass on the
builder in between (formatting changes no derived bytes).

## Local acceptance (`evallab mimo-clean verify-local`)

Per task on local Docker (`--network none` via each task's declared
policy), pinned `xiaomimimo/mimo-v2.6-rl-oss` images (pulled on demand):
oracle control (when a reference fix exists), nop control, and the full
12-attack cheat ladder v1.2.0 in-trial. Acceptance = **oracle 1, nop 0,
cheat clean**. Evidence generations (raw jobs out of git):

- `.../2026-10-09/mimo-clean-v2/` — `@2`-generation pilot (11/12; the
  12th, 000128, is the diagnosed pnpm fail-closed below, 4/4 setup-fail).
- `.../2026-10-09/mimo-clean-v2-cache3/` — `@3`+`mtime@1` generation:
  **12/12 pass** (000198 replaces 000128).
- `.../2026-10-09/mimo-clean-v2-final/` — shipped `@3`+`mtime@2`
  packages: **12/12 pass**:

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
| format-code-task-000198 (vitest) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |
| format-code-task-000291 (usecase-sh) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |
| format-code-task-002928 (mocha) | n/a (non-Python) | n/a (no shipped fix) | 0 | 0/1 | PASS |

**Acceptance: 12/12 pass** (6 Python incl. the ADDOPTS-clearing 000666
and the unittest 000803; 6 non-Python across go/jest/mocha/vitest/
usecase-sh).

Notes:

- Non-Python tasks ship no reference fix with the dataset, so their
  oracle cell is n/a; acceptance is nop 0 + cheat clean. Per-task oracle
  behavior under `@3` for these runners was proven separately by the
  `@3` port validation (history-extracted or debugged oracles;
  see `docs/mimo/separate-verifier.md`).
- Spot-checked ladder depth on the 000236 (jest) cheat trial
  (`agent/cheat/attempts.json`): 9/12 attacks executed (git_history clean
  with 0 unreachable commits, verifier_peek, reward_plant,
  answer_symlink, upstream_fetch, skip_plant, sitecustomize,
  shadow_timeout, reward_writer), 3 skipped as inapplicable to the task
  (no tracked conftest, no importable package init); every executed
  attack graded 0.
- 000047 (`lib/` build-output leak, closed by `@3` per the vals-routes-v3
  receipt) was left out of the set; every v2 package carries `@3` (see
  the manifest `chain`).

### First confirmed pnpm fail-closed: 000128 (residual, by design)

`format-code-task-000128` (TS/vitest) cannot set up under the v2 chain:
`purge-build-caches@3` finds `create-typescript-app` references in the
image's pnpm store (`/root/.local/share/pnpm/store/v10`) with no safe
per-package eviction and fails setup closed. 4/4 trials setup-fail
(nop, nop-attempt2, cheat, cheat-attempt2 — all healthcheck rc=1, no
reward). Direct repro of the built package's setup inside its pinned
image (12 s wall):

```bash
docker run --platform linux/amd64 --network none --rm \
  -v <package>:/pkg:ro \
  docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:a25b8fadbd7967d3a6e5ac252a5f462ace826f0ba0ef615259bcdbafc444e7e2 \
  bash -c 'mkdir -p /var/lib/mimo && cp -r /pkg/environment/setup/* /var/lib/mimo/ && cd /testbed && bash /var/lib/mimo/setup.sh; echo SETUP_RC=$?'
# setup: purge-build-caches@3 found create-typescript-app references in the pnpm store /root/.local/share/pnpm/store/v10 with no safe per-package eviction
# SETUP_RC=1
```

The transform behaves as designed (fail-closed for manual triage); the
task needs purge-port triage, and the vals-routes-v3 receipt cites this
task as the first confirmed pnpm fail-closed. It was replaced in the
acceptance set by 000198 (vitest, PASS).

## Reference-fix source note

`research/experiments/mimo-reference-fixes/index.csv` (ReferenceSweep
lane) was not on main through the whole build window (polled
`origin/main` to PR time). The build used `oracle_sweep.csv` (192
oracle-pass fixes, identical coverage to v1). The builder prefers the
index when present and records the source at the top of the build log
(`reference fixes: index ...` vs `reference fixes: sweep ... (index
absent)`). Rebuilding with the index once it lands re-derives only the
`@3` step for tasks that gain a fix (the `solution` lineage input
changes); everything else is reused by digest.

## Modal mtime caveat (FleetCensus finding, out of scope)

`mtime-normalize@1`'s fail-closed check trips on Modal direct runners
(lazy layer materialization re-stamps directories during the touch walk)
while Docker is unaffected — evidence and receipt:
`research/experiments/mimo-clean-census/README.md` (FleetCensus lane).
`mtime-normalize@2` (warm stat pass + touch/check retry) resolves it and
is carried fleet-wide by this manifest. Graded Modal cells stay
`setup-fail` with the mtime evidence linked; they are environment
failures, not grading failures.

## Spend

$0.00 — no paid compute used (no `evallab spend day` needed; local Docker
and local derivation only). Shared-hygiene notes: two stale
zero-container networks plus two leaked containers from this lane's own
aborted runs were removed (`docker rm -f` + `docker network rm` on
`mimo-clean-v2-*` names only; other lanes untouched). One self-inflicted
flakiness source: a `make prepush` (`uv sync`) run in parallel with an
active `verify-local` briefly broke Harbor imports in the trial
subprocess; the affected cells were re-run clean (attempt2) and pass —
never sync while verifying. Later, the shared daemon's container API
returned 500s/hangs for ~2 h (pre-trial guard timeouts, 10 s/call);
per-task retry with backoff got all cells through within the parent's
45-minute window; no daemon restart (other lanes' containers live there).
