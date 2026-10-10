---
status: living
audience:
  - builder
  - analyst
  - runner
---

# mimo-clean-v2 clean set

The canonical clean task set for the MiMo code pool, all languages: one
derived package per Python task the ledger marks usable (`keep`/`fix`;
`discard` rows are skipped with reason) plus one per non-Python snapshot
task, each carrying the full clean chain in its lineage.

- Version id: `mimo-clean-v2`
- Manifest (tracked, small): `research/experiments/mimo-clean-v2/manifest.csv`
- Builder: `src/evallab/mimo_clean.py`
- CLI: `evallab mimo-clean build|verify-local`
- Acceptance receipt: `research/experiments/mimo-clean-v2/README.md`
- Predecessor: `research/experiments/mimo-clean-v1/` (manifest + README kept
  as history; v1 covered the Python pool only, ending at
  `purge-build-caches@1` + `separate-verifier@2`).

## The canonical Python chain

```
ledger run package (repairs)
  -> strip-future-history@1
  -> purge-installed-copies@1   (scoped; see below)
  -> purge-build-caches@2       (@3 when the node build-output port lands)
  -> mtime-normalize@1
  -> separate-verifier@3        (last; + solution/solve.sh from the reference fix)
```

| Step | Transform | Closes | Why it is in the chain |
|---|---|---|---|
| 1 | `strip-future-history@1` | V1 future history on branches, V2 unreachable git objects | Rebuilds agent-visible git storage from exactly BASE and its ancestors; without it 67% of code tasks leak the answer through git objects. |
| 2 | `purge-installed-copies@1` | E1 installed/build copies of the fixed project | Removes `build/`, project egg-info and site-packages copies, then reinstalls the base tree editable offline. Scoped to validated targets (below): the block fails closed at setup, and unvalidated projects would break setup instead of leaking. |
| 3 | `purge-build-caches@2` | V5 build/module caches | The `@1` sweep plus this project's own entries in shared caches (pip wheels, Go module/build cache, cargo target + registry copies, Maven artifacts, Gradle project cache, npm/yarn/pnpm entries), each fail-closed. `@3` adds node gitignored build-output handling (the 000047 `lib/` leak); the builder prefers `@3` once `purge_build_caches` ships it and records whichever generation it used in the manifest `chain`. |
| 4 | `mtime-normalize@1` | V3 file mtimes pointing at fixed files | Touches the worktree to one stamp so `find -newermt` cannot rank the fixed files. |
| 5 | `separate-verifier@3` | E2 grader tamper, all runners | Patch-only grading in a pristine verifier checkout with structured per-runner checks (go/jest/mocha/vitest/phpunit/rspec/cargo/…; see `docs/mimo/separate-verifier.md`), cross-language tamper gates, new-file config drops, and the Python residuals (unittest parse, ADDOPTS-clearing conftest hook). Keeps every `@2` guarantee byte-for-byte for Python/pytest. It is last because it bundles the parent's clean setup chain into the verifier image. |

The builder reuses the existing `derive_*` functions in order and records
each step's lineage via `evallab.task_variants.derive_task`. Steps the run
package already carries (repair rows ship strip/purge) are kept, not
re-derived. A single mechanical step that raises `VariantInvalid` is
skipped with reason while the rest of the chain continues (same tolerance
as v1); strip failure is fatal for that task.

## The canonical non-Python chain (all 1,519 tasks)

```
snapshot task dir
  -> strip-future-history@1
  -> purge-build-caches@2 (or @3, same rule as above)
  -> mtime-normalize@1
  -> separate-verifier@3
```

`purge-installed-copies` is a Python pip mechanism and never applies to
these tasks; the manifest `reason` notes it (`n/a to <language>`), it is
not derived. Snapshot tasks whose `task.toml` category is unreadable, and
Python-category snapshot tasks without a ledger row, are skipped with
reason (ledger membership always wins: e.g. 002361 is snapshot-category Go
but builds through the Python chain from its ledger run package).

## Purge scoping (read this before widening)

`purge-installed-copies@1` applies to exactly two groups:

1. **CONFIRMED_PURGE** (`format-code-task-001269`, `format-code-task-002308`):
   installed-copy leaks confirmed by reading the image. Both run packages
   already carry the marker, so the chain keeps it.
2. **Already-carried run packages**: repair rows whose setup already applies
   the marker (validated by the repair lane).

Everything else skips with reason:

- **Fail-closed** (`exploit_probe.PURGE_INAPPLICABLE`, HAR-194): 002552
  (poetry on py3.8), 000792/002486 (no identifiable project name), 002139
  (bitbake), 002391 (PEP 668).
- **Out of scope**: all remaining Python tasks. HAR-185 confirmed 0 of 100
  sampled images outside the two confirmed targets; the vals-routes-v2
  census adds five tasks where purge would break setup
  (002938/000666/000905/001198/000324) and they stay scope-skips — the
  block fails setup closed rather than leaking, so blind application would
  trade unproven leaks for certain setup breakage.

## Reference fixes and markers

- The manifest's `reference_fix` prefers
  `research/experiments/mimo-reference-fixes/index.csv`
  (`task_id,label,fix_commit,patch_path,patch_sha256,source`; absolute
  patch paths, bytes outside the repo; only oracle-pass rows carry one)
  when that file exists, else the HAR-191
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
- The `@3` probe marker is auto-derived from the hidden test patch
  (`tests/test.patch`): first added `test_*` function name, else the first
  touched file's basename, else the task id, sanitized for the probe hook's
  grep pattern.

## The `verify` column

The manifest's `verify` column is the fleet-census grade per clean
package, fed later by the census lane. Every v2 row ships as
`unverified`; the census updates the field in place (values it defines).
Do not infer package quality from `status=built`: built means the chain
derived, verified means it graded clean.

## How to rebuild

```bash
git fetch origin main
git worktree add ~/Developer/eval-lab/.worktrees/mimo-clean -b mimo-clean-v2 origin/main
cd ~/Developer/eval-lab/.worktrees/mimo-clean
uv sync --frozen --extra laminar
uv run --extra laminar evallab mimo-clean build
```

The build is deterministic and idempotent: every step reuses the existing
lineage record for `(task, transform, parent digest)` and derives
(content-addressed) only what is missing, so re-running yields the same
`final_digest` values and never duplicates records. Snapshot parents
resolve through the primary checkout's task store (worktrees do not carry
it); pass `--snapshot` to point elsewhere. Verify a rebuild with:

```bash
uv run evallab mimo-clean build --tasks <id1,id2,...>
# compare final_digest against research/experiments/mimo-clean-v2/manifest.csv
```

A single task (or subset) rebuilds with `--tasks a,b --workers N`.

Reference fixes resolve at build time: `--reference-index` (default the
tracked index path; falls back to `--sweep` + `--results-home` when the
file is absent). Tasks that gain an oracle fix between builds re-derive
only the `@3` step (the `solution` lineage input changes); everything else
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
  --tasks <id1,id2,...> --jobs-dir runs/mimo-clean-v2
```

Per task it runs, on local Docker with the task's pinned image: the oracle
control (when a reference fix exists), the nop control, and the full
12-attack cheat ladder, then prints a per-task acceptance row. Acceptance is
**oracle 1, nop 0, cheat clean** (zero cracked trials). Raw jobs stay out of
git; the acceptance table is recorded in
`research/experiments/mimo-clean-v2/README.md`.

## Residual gaps (honest)

- Installed copies outside CONFIRMED_PURGE rely on the HAR-185 sample
  (0/100) plus the vals-routes-v2 census; the fail-closed tasks keep their
  copies by design. If a new installed-copy leak is confirmed anywhere, add
  the task id to `CONFIRMED_PURGE` and rebuild.
- The 000047 `lib/` build-output leak stays open until
  `purge-build-caches@3` lands and the set rebuilds (the manifest `chain`
  shows which generation each package carries).
- Tasks without a reference fix skip the oracle cell; their acceptance is
  nop 0 + cheat clean, and solvability still rests on the census evidence
  cited in the ledger (Python) or the `@3` port-validation oracles
  (non-Python, hand-built per task, not shipped).
- `@3` keeps its documented residual R1: tracked `Makefile`/CMake/target
  definitions are never touched, so the 000898-style exit-header tamper in
  tracked code scores 1 under `@3` by design.
- Every row is `verify=unverified` until the fleet census grades the
  packages; the `@3` port validation (86 cells) and the v2 local
  acceptance (≥12 tasks) are samples, not fleet proof.
