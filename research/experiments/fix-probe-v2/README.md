# Fix-content probe v2 (256 reference tasks + 196 v4 probe-pending)

Owner: ProbeV2. $0 local Docker (`--network none`, cached registry images).
No model calls. Paid spend: **$0.00** (cap $1.50, untouched).

## Why v2

The v1 census check (d) over reference tasks (prior run `probes-full-228` +
`probe-pilot-3`, 231 tasks, Modal $0.70) reported 65 clean-chain signature
hits and 164 probe-blind tasks: observed line-pattern matches, not
established leaks. Hand triage (`~/Developer/eval-lab-results/2026-10-10/leaktriage/`,
LeakTriage verdicts-q1/q2/q3.md) showed every sampled hit was a false
positive: generic lines (`except (OSError, AttributeError):`,
`from copy import deepcopy`), Modal-only `/__modal` paths, base-subtraction
shadowing (000122), and patternless fixes needing blob-SHA (deletion-only
001955, single 18-char line 002378, with `reconstruct_blob.py` proof).

## What v2 changes (`src/evallab/fix_content_census.py`)

- Post-fix blob SHA + whitespace-normalized variant per changed non-test
  file, derived container-side (`git archive` base + `git apply` reference
  patch in scratch `/tmp/probe-pfx-*`, excluded from all scans); searched
  in same-basename carriers (incl. `*.pyc` stems) and `.git` objects.
- Per-pattern base subtraction; imports + short lines excluded (host +
  staged awk agree); generic idioms (one pattern in >= 3 unrelated dirs)
  dropped; a location counts with >= 2 co-located fix-specific lines or a
  blob hash.
- Harness paths excluded (`/__modal`, `/proc`, `/sys`) + own scratch.
- Published fast control (`no_scan`): fix commit or post-fix blob pre-setup
  counts, else `blind` with reason.
- Census integration kept: Modal backend + `egress_lock` in `run_probe`;
  `mimo_census` blind/positive logic and `census_locations` now use the v2
  `control`/`real_hits` fields.

## Validation (8 hand-triaged tasks)

`validation/` raw root: `~/Developer/eval-lab-results/2026-10-10/fix-probe-v2/validation/`
(+ `smoke/` for the 000161 pilot). All 8 match LeakTriage exactly: 0 real
hits on clean, positive published controls. Notable: 000122's worktree
shadowing hits gone (per-pattern subtraction), 000219's 36 hits gone
(imports excluded), 001955/002378 clean via blob-absence with positive blob
controls, worktree-at-base notes throughout.

## Fleet run (256 reference tasks, published + clean)

Command (16-task shards, 2 concurrent local trials max):

```sh
uv run --extra laminar python -m evallab.fix_content_census batch \
  --primary ~/Developer/eval-lab \
  --manifest research/experiments/mimo-clean-v3/manifest.csv \
  --ledger research/experiments/python-task-ledger/ledger.csv \
  --index research/experiments/mimo-reference-fixes/index.csv \
  --results ~/Developer/eval-lab-results/2026-10-10/fix-probe-v2/fleet \
  --tasks <shard>
```

Raw root: `~/Developer/eval-lab-results/2026-10-10/fix-probe-v2/fleet/`
(`<task>.meta.json` rows + `out-published/`/`out-clean/` probe outputs).

Result: 256/256 probed. Published probe-blind **164 → 0**. Clean:
22 tasks with real hits, 1 blind (002389, clean setup rc=1 deterministic —
purge-build-caches@2 precondition; applicability finding, flagged to the
clean-set owner), 4 tasks with no clean package (published-only rows:
000102/000163/000200/000203). Hand triage of all 22 (derivation rule:
byte-identical post-fix bytes prove planted content; line matches judged
for fix substance): **11 REAL E1 leaks** (000255, 000322, 000466, 000497,
000545, 000812, 002371, 002384, 002668, 002790, 002829 — proof:
`~/Developer/eval-lab-results/2026-10-11/fix-probe-v2/real-leaks.md`),
5 answer-visible via env tooling/deps (001478, 001671, 001945, 001971,
002414 — reference implementation readable from legitimate deps/tooling,
not purge-removable; 002414 confirmed @2-by-design by PurgeV2), 6 probe
coincidences (000636 stdlib ssl idiom, 000926 upstream markdown text,
001433 babel guard, 002549 editable-install README examples without
implementation, 002786 CI boilerplate, 003057 changelog hint without
implementation). Follow-up: purge-installed-copies@2 (PR #836) closes all
11 E1 on the final chain (PurgeV2 verified); this slice changes no transforms.

## v4 probe-pending run (109 new-index tasks, v4 packages)

Same command with `--manifest research/experiments/mimo-clean-v4/manifest.csv`
and `--results .../fleet-v4` (+ snapshot fallback for non-ledger run packages;
see `run_batch`). Raw root:
`~/Developer/eval-lab-results/2026-10-10/fix-probe-v2/fleet-v4/`.
Result: 109/109 probed. Published blind 8, clean blind 8 (same 8:
000169/000170/000182/000384/000385/000398/000414/000415, `no-fix-signal` —
patternless/patch-apply-failed with no fix objects in image; stay pending).
8 real-hit tasks triaged: **1 REAL** (000919 — baked `lib/` build output with
fix content, node build-output purge gap), 1 V6 residual (001690 — newer env
keras contains equivalent backend calls), 6 coincidences (000312 django-admin
idioms, 000477 lockfile version lines without implementation, 000734 public
arange math, 000784 dependency API declarations, 000943 BFS idiom, 001425
stripe doc URLs).

## Manifest updates (this PR)

- v3 `verify`: 20 refuted `fail:open-leak` → `pass`; 6 new real leaks →
  `fail:open-leak` (11 total: 5 confirmed + 6 new).
- v4 `verify`: 184 of 196 `unverified:probe-pending` resolved (176 `pass`,
  8 `fail:open-leak`: 000255/000322/000466/000497/000545/000812/000919/001690);
  12 stay pending (4 no clean package, 8 blind).
- Per-task verdicts (chain/digest-bound, for v5): `verdicts.json` in this dir
  (365 rows: 334 clean, 12 real-leak, 5 residual, 1 by-design, 9 blind,
  4 no-clean-package).

## Spend actuals

- Local Docker pulls/runs: $0.00 (images cached, re-pulled after Main's
  2026-10-11 eviction as needed; pulled images removed after use).
- Paid (Modal/Daytona/model): $0.00. Cap $1.50 untouched.
- Approval: Peter 2026-10-09 chat ("feel free to spend up to $25 on stuff");
  actor `omp-mimo-clean (delegate for Peter)`.
