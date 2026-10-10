# mimo-clean-v4: canonical clean set with separate-verifier@6 (all languages)

Date: 2026-10-10. $0 paid compute (local Docker + local derivation only;
no model calls, no Modal/Daytona).

## What this is

One clean package per code task, each carrying the canonical chain in its
lineage — the v3 set with `separate-verifier@6` replacing `@5`
(pristine-baseline fail-to-pass grading; see
`docs/mimo/separate-verifier.md`), built against all reference fixes on
main at build time.

- Version id: `mimo-clean-v4`
- Manifest (tracked, small): `research/experiments/mimo-clean-v4/manifest.csv`
- Builder: `src/evallab/mimo_clean.py` (same chain code; verifier
  generation resolves `@6`)
- Predecessor: `research/experiments/mimo-clean-v3/` (manifest + README
  kept as history; v3 ended at `separate-verifier@5`)

## Why @6 (the hole it closes)

`@5` graded failures/errors only and ignored skips, arguing source-level
skips were equivalent to hard-coding. They are not: a call-time skip
planted in the imported package (`pytest.Function.runtest` patched to
`pytest.skip`, no test knowledge, no tamper-signature match) graded
**reward 1 with zero tests passing** on a `@5` package — measured live on
local Docker: 002552 `rc=0 cases=12 bad=0 skipped=12 named=4 missing=[]`
(`cheat-verdicts.json`: verdict `cracked`, reward 1.0).

`@6` runs the hidden tests on its own pristine tree first (test patch
applied, no agent bytes) and requires every pristine failure/error to
PASS (not skip) with no pristine pass failing; skips are tolerated only
where pristine skipped. Absent baseline keeps `@5` rules; go-test gains a
non-empty-run rule. The same 002552 attack on the `@6` package grades
`fail-to-pass: pristine outcomes not preserved`, reward 0. Full proof
table in `docs/mimo/separate-verifier.md`.

Two `@5` defects fixed along the way: the embedded loader took the
missing-report path on any plugin-report `open()` failure before
consulting the per-phase archives (ADDOPTS-cleared oracles such as 000666
always graded 0), and a mistimed timing `echo` forced rc 0 for every
trial.

## Build result (`evallab mimo-clean build --workers 4`)

Reference fixes resolved from the landed reference-fix index at build
time: `research/experiments/mimo-reference-fixes/index.csv`
(973 rows, 564 fixes; sha256
`d701351fe18b6036cf2cb3ec983f0caf561e805cc8a6e0129e49c060bb0b8962`,
landed by #828, merge `1497fdbb6`).

| Status | Count | Detail |
|---|---|---|
| built | 2666 | 1,148 Python keep/fix rows + 1,518 non-Python snapshot tasks |
| skipped | 32 | all `discard` verdicts, reason cites the ledger row |

Chain shapes over the 2,666 built:

| Chain | Tasks |
|---|---|
| `strip-future-history@1>purge-build-caches@4>mtime-normalize@2>separate-verifier@6>agent-network-none@1` | 2664 (1,146 Python + 1,518 non-Python) |
| `strip-future-history@1>purge-installed-copies@1>purge-build-caches@4>mtime-normalize@2>separate-verifier@6>agent-network-none@1` | 2 (`format-code-task-001269`, `format-code-task-002308`: purge carried in their repair run packages) |

Per-domain counts (manifest `language`): identical to v3 (python 1148,
go 721, javascript 388, typescript 166, unknown 130, ruby 25, php 23,
java 22, c++ 13, c 7, rust 7, scala 6, kotlin 3, dart 3,
lua/elixir/swift/svelte 1 each).

- 564 built tasks carry an oracle-pass reference fix
  (`solution/solve.sh` built from the indexed patch; manifest
  `reference_fix` + `oracle_label`): 428 Python, 136 non-Python. v3
  carried 256; the +308 come from the recovery + Go/JS-TS waves (#828).
- Every row ships `verify=unverified`: the fleet census grades the
  packages later. `built` means the chain derived — it does NOT mean the
  package sets up.

## Determinism check

For `format-code-task-000163` and `format-code-task-002552`, the
`separate-verifier@6` + `agent-network-none@1` records were deleted and
re-derived: both final digests are byte-identical to the manifest rows
(`c0bdcba39e05…`, `4d828ebf812…`). Upstream stages are frozen v3 inputs
and were reused, so this checks `@6` determinism given fixed parents.

## Local acceptance (final `@6` packages, local Docker, $0)

Per task (agent `no-network`, verifier public), pinned
`xiaomimimo/mimo-v2.6-rl-oss` images. Acceptance is **oracle 1, nop 0,
cheat clean**. Raw jobs stay out of git.

| Task (runner) | oracle | nop | cheat |
|---|---|---|---|
| 000163 (pytest, pass+skips) | 1 | 0 | — |
| 000200 (pytest, multi-phase) | 1 | 0 | — |
| 000203 (pytest, dep-skips) | 1 | 0 | — |
| 002552 (pytest, ADDOPTS-cleared) | 1 | 0 | skip attacks 0 (proof table above) |
| 000666 (pytest, ADDOPTS-cleared) | 1 | 0 | — |
| 000114 (pytest, baked `.venv`) | 1 | 0 | — |
| 000102 (pytest, rootdir shift) | 1 | 0 | — |
| 000803 (unittest) | 1 | 0 | skiptest 0 |
| 000553 (go-test) | 1* | 0 | — |

*000553 has no shipped reference fix. Its oracle cell comes from a
temporary control package copied from the final canonical Go package,
adding only `solution/solve.sh` with the real image-history `main.go`
at commit `53b42c1` (same arrangement as the `@4` receipt); reward **1**.
The canonical package still has `reference_fix=none`.

Grading-time overhead: `baseline_sec=2 agent_sec=2` on the 002552 skip
trial — about one extra pristine test run.

## Re-grading v3 -> v4 (for the fleet census)

v3 (`@4`) rows are unaffected by the skip hole: `@4` counted skips as
bad, so no v3 pass can be a skip-attack artifact. v4 needs regrade of:

- tasks whose v3 oracle was `fail:0` due to skips (the `@5`-motivating
  shapes: pass+skip legit oracles) or multi-phase reports;
- tasks carrying a new oracle fix (the +308 since v3): their v3 rows
  predate the fix.

The census owns the fleet grading; this manifest only swaps the grader.

## Spend

$0.00 — no paid compute used (local Docker and local derivation only).
