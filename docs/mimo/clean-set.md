---
status: living
audience:
  - builder
  - analyst
  - runner
---

# mimo-clean-v5 clean set

The canonical clean task set for the MiMo code pool, all languages: one
derived package per Python task the ledger marks usable (`keep`/`fix`;
`discard` rows are skipped with reason) plus one per non-Python snapshot
task, each carrying the full clean chain in its lineage.

- Version id: `mimo-clean-v5`
- Manifest (tracked, small): `research/experiments/mimo-clean-v5/manifest.csv`
- Builder: `src/evallab/mimo_clean.py`
- CLI: `evallab mimo-clean build|verify-local`
- Acceptance receipt: `research/experiments/mimo-clean-v5-census/README.md`
- Predecessors (kept as history; every README carries a superseded pointer —
  do not build on them): `research/experiments/mimo-clean-v4/` (@6),
  `mimo-clean-v3/` (@4/@5 era), `mimo-clean-v2/`, `mimo-clean-v1/`
  (Python pool only).
- What changed since v4: `purge-installed-copies@2` replaces the scoped `@1`
  for **all** Python tasks (neutralize-in-place; receipt
  `research/experiments/purge-installed-copies-v2/README.md`) and
  `separate-verifier@8` replaces `@6` (G-shape certification plus the
  terse/dashed follow-ups; see `docs/mimo/separate-verifier.md`). The
  cache slot stays `purge-build-caches@4` fleet-wide; 20 named node-build
  tasks fold onto `purge-build-caches@5` per row (below). Manifest rows for
  five tasks carry `excluded-from-training` in `reason` (answer-visible
  through legitimate environment dependencies; the v5 census marks them
  `fail:open-leak:env-dependency`), plus 000928
  (`fail:open-leak:node-build-pnpm`).


## The canonical Python chain (all 1,148 tasks)

```
ledger run package (repairs)
  -> strip-future-history@1
  -> purge-installed-copies@2   (all Python tasks; no-op when nothing matches)
  -> purge-build-caches@4
  -> mtime-normalize@2
  -> separate-verifier@8        (+ solution/solve.sh from the reference fix)
  -> agent-network-none@1       (agent phase only; verifier remains public)
```

| Step | Transform | Closes | Why it is in the chain |
|---|---|---|---|
| 1 | `strip-future-history@1` | V1 future history on branches, V2 unreachable git objects | Rebuilds agent-visible git storage from exactly BASE and its ancestors; without it 67% of code tasks leak the answer through git objects. |
| 2 | `purge-installed-copies@2` | E1 installed/build copies of the fixed project | Neutralizes installed copies in place (tracked copies overwritten with BASE bytes, fix-added files deleted) for **all** Python tasks; no-ops when nothing matches. Tops come from `git ls-files` at BASE (no project-name resolution, so poetry/name-less trees work); the block verifies with a re-scan plus a pre/post import gate. See below. |
| 3 | `purge-build-caches@4` | V5 build/module caches | Retains `@3`'s language-aware and node-build-output purge, while allowing a cache explicitly disabled by its tool (Modal pip cache). Unknown cache failures still fail closed. Pinned to the landed `@4`; historical v2 packages keep `@3`. |
| 4 | `mtime-normalize@2` | V3 fix-bearing mtimes | Touches the worktree to one stamp, warms lazy layers, and retries touch+check so Modal materialization timestamps cannot trip the fail-closed check. Shipped in PR #813; the builder resolves the active mtime generation and records it per manifest row. |
| 5 | `separate-verifier@8` | E2 grader tamper, all runners | Patch-only grading with structured runner checks, tamper gates, config drops, rootdir-robust junit matching, complete pristine-workdir deltas, skip-tolerant grading, multi-phase junit union, G-shape certification (fail-to-pass shapes that legitimately pass), terse-reporter and dashed-module certification (see `docs/mimo/separate-verifier.md`). Runs after all setup-cleaning steps so the verifier inherits the clean setup. |
| 6 | `agent-network-none@1` | V6 upstream fetches | Appends `[agent] network_mode = "no-network"` after verifier derivation. Setup and the separate verifier retain the public baseline; only the agent phase is locked. See the `agent-network-none` receipt and `docs/mimo/vals-routes.md` for backend enforcement. |

## The canonical non-Python chain (all 1,518 tasks)

```
snapshot task dir
  -> strip-future-history@1
  -> purge-build-caches@4
  -> mtime-normalize@2
  -> separate-verifier@8
  -> agent-network-none@1
```

`purge-installed-copies` is a Python pip mechanism and never applies to
these tasks; the manifest `reason` notes it (`n/a to <language>`), it is
not derived. Snapshot tasks whose `task.toml` category is unreadable, and
Python-category snapshot tasks without a ledger row, are skipped with
reason (ledger membership always wins: e.g. 002361 is snapshot-category Go
but builds through the Python chain from its ledger run package).

## Purge coverage (@2: all Python tasks)

`purge-installed-copies@2` applies to every Python row (no scope checks, no
fail-closed skips): the v3/v4 `@1` scoping is gone. A carried `@1` block is
excised and recorded as `superseded_v1` instead of stacking. Full design,
closure proof (11/11 probe-v2 E1 carriers neutralized to BASE bytes, setup
rc=0 everywhere) and oracle/nop validation (19/19 PASS, local Docker) are in
`research/experiments/purge-installed-copies-v2/README.md`.

Known residuals (documented there, not re-derived here):

- Untracked compiled artifacts (`.so`/`.pyd`) under project dirs stay.
- pipx/pip tooling homes stay: five tasks remain answer-visible through
  legitimate environment dependencies (`001478`, `001671`, `001945`,
  `001971`, `002414`); their manifest rows carry `excluded-from-training`
  and the census marks them `fail:open-leak:env-dependency`.
- Wheels/sdists under `dist/` are not entered.

## Node build-output fold (@5 for 20 named tasks; fleet stays @4)

`purge-build-caches@5` generalizes the node build-output section to any
depth (monorepo `packages/*/lib|dist`, `es`/`esm`/`cjs`; per-scope rebuild
or delete, else fail closed). Diagnosis, class table and validation:
`docs/mimo/node-build-leak.md`. The canonical v5 chain keeps `@4`
fleet-wide for digest-stability (a uniform @5 rebuild churns every
`setup.sh` text and would invalidate all banked census cells — measured:
2666/2666 digests change with agent-tree bytes identical; see the v5
receipt); 20 tasks fold onto @5 per row (`NODE_FOLD_TASKS` in
`src/evallab/mimo_clean.py`, per-row `chain` records `@5` honestly):
3 probe-confirmed REAL leaks (`000919`, `000435`, `000170`, re-graded as
fold gates) and 17 fix-path-shape at-risk tasks (guard-gated setups,
labeled `unverified:node-build-fold`; `000632`/`001763` stay on @4 after
guard failures, labeled `unverified:node-build-leak`). `000928`
(pnpm-blocked) is excluded
from training and marked `fail:open-leak:node-build-pnpm`. A future rebuild
with budget for full re-verification can go uniform @5.

## Verifier fold (@9 for 001431; fleet stays @8)

`separate-verifier@9` excuses agent skips inside a satisfied
collection-error module (first executions the interrupted pristine run
never recorded). Diagnosis, ground truth (locally captured pristine
junit), and validation: `docs/mimo/separate-verifier.md` (`@9` section).
The canonical v5 chain keeps `@8` fleet-wide for digest-stability (a
uniform `@9` rebuild churns every `test.sh` and would invalidate all
banked census cells); one task folds onto `@9` per row
(`SEPARATE_V9_TASKS` in `src/evallab/mimo_clean.py`, per-row `chain`
records `@9` honestly): `001431`, whose working reference fix graded 0
under `@8` (re-graded oracle 1 / nop 0 plus a clean 1.4.0 ladder on the
`@9` digest). `@9` only ever clears fail-to-pass (never sets it), so the
fold keeps every `@8` guarantee. A future rebuild with budget for full
re-verification can go uniform `@9`.

## Verifier fold (@10 for 000245+000048; fleet stays @8/@9)

`separate-verifier@10` anchors the pristine tree on the first
`test.sh` invocation and resets to it on every repeat, so Harbor's
`RepeatVerifier` (`repeat_n=3`, same verifier container) starts each
run from identical bytes. Diagnosis and validation:
`docs/mimo/separate-verifier.md` (`@10` section). Two opaque-wrapper
tasks fold onto `@10` per row (`SEPARATE_V10_TASKS` in
`src/evallab/mimo_clean.py`, per-row `chain` records `@10` honestly):
`000245` (TypeScript) and `000048` (Go), whose hidden patches create
`mimo_build_env.tar.gz.b64`/`mimo_test_command.sh` in the workdir and
whose `@8` packages failed `repeat/1` with `already exists` when the
dirty tree was recaptured as `PRISTINE`. `@10` keeps parts B/C
byte-identical to `@9`, so every `@8`/`@9` grading guarantee (incl.
001431 on `@9`) is kept. A future rebuild with budget for full
re-verification can go uniform `@10`.

## Reference fixes and markers

- The manifest's `reference_fix` prefers
  `research/experiments/mimo-reference-fixes/index.csv` (973 rows, 564 with
  patches: `task_id,label,fix_commit,patch_path,patch_sha256,source`;
  absolute patch paths, bytes outside the repo; only oracle-pass rows carry
  one) when that file exists, else the HAR-191
  `research/experiments/python-task-ledger/oracle_sweep.csv` rows labeled
  `oracle:pass*` whose solution patch file exists. The builder records
  which source it used at the top of the build log.
- `solution/solve.sh` embeds that patch base64 and applies it with
  `git apply` at the agent worktree repo root, so `evallab run --agent
  oracle` grades 1. It is added only when the parent has no solution
  (overwrite refused by the transform).
- Non-Python tasks ship no reference fixes with the dataset
  (`reference_fix=none`); their oracle cell is n/a and acceptance is
  nop 0 + cheat clean.
- The probe marker is auto-derived from the hidden test patch
  (`tests/test.patch`): first added `test_*` function name, else the first
  touched file's basename, else the task id, sanitized for the probe hook's
  grep pattern.

## The `verify` column

The manifest's `verify` column is the fleet-census grade per clean
package, fed by the census lane (`evallab mimo-census run|report`, receipt
`research/experiments/mimo-clean-v5-census/`; grading rules in
`docs/mimo/verification.md`). Every v5 row ships as `unverified`; the
census updates the field in place (values it defines), with triaged labels
in the receipt's `verify-overrides.csv` so re-running `report` never loses
them. Do not infer package quality from `status=built`: built means the
chain derived, verified means it graded clean.

Grades are bound to `(task_id, final_digest)`. Rebuilding the same package
preserves its grade; changing any package bytes resets it to `unverified`.
`--reset-verify` also clears same-digest grades. The #848 v5 digest refresh
invalidated 2,664 old labels (including 14 passes): see the census
`digest-refresh.csv`. Historical failures remain admission holds, not fresh
measurements. Never re-pin an override to new package bytes without evidence.

## How to rebuild

```bash
git fetch origin main
git worktree add ~/Developer/eval-lab/.worktrees/mimo-clean-v5 -b mimo-clean-v5 origin/main
cd ~/Developer/eval-lab/.worktrees/mimo-clean-v5
uv sync --frozen --extra laminar
uv run --extra laminar evallab mimo-clean build --clean-version mimo-clean-v5
```

The build is deterministic and idempotent: every step reuses the existing
lineage record for `(task, transform, parent digest)` and derives
(content-addressed) only what is missing, so re-running yields the same
`final_digest` values and never duplicates records. Snapshot parents
resolve through the primary checkout's task store (worktrees do not carry
it); pass `--snapshot` to point elsewhere. Verify a rebuild with:

```bash
uv run evallab mimo-clean build --tasks <id1,id2,...>
# compare final_digest against research/experiments/mimo-clean-v5/manifest.csv
```

A single task (or subset) rebuilds with `--tasks a,b --workers N`.

Reference fixes resolve at build time: `--reference-index` (default the
tracked index path; falls back to `--sweep` + `--results-home` when the
file is absent). Tasks that gain an oracle fix between builds re-derive
only the final verifier step (the `solution` lineage input changes); everything else
is reused by digest.

## Lineage records: why only the manifest is committed

The full build derives ~10k new lineage JSONs (~200 MB: each record
inlines the changed setup/task.toml/test files) into
`library/task-variants/`. Committing that in one PR is not reviewable and
duplicates bytes the builder reproduces deterministically, so this slice
commits only the manifest plus the reproducible build command above. The
manifest pins every `final_digest`, so any rebuild is verifiable
byte-for-byte. (Rule used: commit lineage iff the new total stays under
~20 MB.)

## How to verify

```bash
uv run --extra laminar evallab mimo-clean verify-local \
  --tasks <id1,id2,...> --jobs-dir runs/mimo-clean-v5
```

Per task it runs, on local Docker with the task's pinned image: the oracle
control (when a reference fix exists), the nop control, and the full
14-attack cheat ladder (v1.4.0), then prints a per-task acceptance row. Acceptance is
**oracle 1, nop 0, cheat clean** (zero cracked trials). Raw jobs stay out of
git; the fleet acceptance table is recorded in
`research/experiments/mimo-clean-v5-census/README.md`.

## Residual gaps (honest)

- Installed copies are neutralized by `purge-installed-copies@2` on every
  Python task (closure proof 11/11 in the purge-v2 receipt); the five
  `excluded-from-training` tasks keep their environment copies by design
  (pipx/pip tooling homes) and are excluded from training, not graded as
  clean.
- The 000047 `lib/` build-output leak is closed by `purge-build-caches@3`
  (vals-routes-v3 receipt: rebuild via `npm run compile`, oracle 1 / nop 0;
  every v5 package carries `@4` — see the manifest `chain`).
- First confirmed pnpm fail-closed: `format-code-task-000128` (TS/vitest)
  cannot set up under the v2/v3 chain — `@3` finds `create-typescript-app`
  references in the image's pnpm store
  (`/root/.local/share/pnpm/store/v10`) with no safe per-package eviction
  and fails setup closed (4/4 trials: nop, nop-attempt2, cheat,
  cheat-attempt2, all healthcheck rc=1; Docker repro of the built setup in
  12 s — see the v2 README receipt). The transform behaves as designed;
  the task needs manual purge-port triage. Setup-failing packages are the
  reason every manifest row ships `verify=unverified`: the fleet census
  grades them into its own `setup-fail` bucket (unscored rewards with trial
  errors), not as grading failures.
- Tasks without a reference fix skip the oracle cell; their acceptance is
  nop 0 + cheat clean, and solvability still rests on the census evidence
  cited in the ledger (Python) or the `@3` port-validation oracles
  (non-Python, hand-built per task, not shipped).
- `@4` inherits `@3`'s documented residual R1: tracked `Makefile`/CMake/target
  definitions are never touched, so the 000898-style exit-header tamper in
  tracked code scores 1 under `@4` by design.
- Every row is `verify=unverified` until the fleet census grades the
  packages. The `@3` port validation (86 cells) and v2/v3 local acceptance
  samples are not fleet proof.
- Named-ID grading can change for 223/1,148 built Python tasks whose
  command pins `*.py::...`: rootdir-shifted and class-level selections now
  match correctly. The v3 receipt lists them. The independent pristine
  workdir fix can also change grading for baked untracked dependencies;
  the remaining tasks are not claimed reward-equivalent by construction.
