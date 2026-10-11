---
status: living
audience:
  - builder
  - analyst
  - runner
---

# mimo-clean-v3 clean set

The canonical clean task set for the MiMo code pool, all languages: one
derived package per Python task the ledger marks usable (`keep`/`fix`;
`discard` rows are skipped with reason) plus one per non-Python snapshot
task, each carrying the full clean chain in its lineage.

- Version id: `mimo-clean-v3`
- Manifest (tracked, small): `research/experiments/mimo-clean-v3/manifest.csv`
- Builder: `src/evallab/mimo_clean.py`
- CLI: `evallab mimo-clean build|verify-local`
- Acceptance receipt: `research/experiments/mimo-clean-v3/README.md`
- Predecessor: `research/experiments/mimo-clean-v2/` (manifest + README kept
  as history; v2 ended at `separate-verifier@3`). v1
  (`research/experiments/mimo-clean-v1/`) covered the Python pool only,
  ending at `purge-build-caches@1` + `separate-verifier@2`.

> **Successor:** `mimo-clean-v4` is the same chain with
> `separate-verifier@6` replacing `@5` (pristine-baseline fail-to-pass;
> see `docs/mimo/separate-verifier.md`), built with all reference fixes
> on main at build time. Manifest:
> `research/experiments/mimo-clean-v4/manifest.csv`, receipt:
> `research/experiments/mimo-clean-v4/README.md`. The cheat ladder is 14
> attacks from v4 on (`tamper_source_skip`, `tamper_source_skiptest`).
> This page otherwise documents v3 exactly as built and is frozen apart
> from this pointer.

> **Successor:** `mimo-clean-v5` is the same chain with
> `separate-verifier@7` replacing `@6` (G-shape certification; see
> `docs/mimo/separate-verifier.md`). Builder: `evallab mimo-clean build
> --clean-version mimo-clean-v5` (default `--manifest`
> `research/experiments/mimo-clean-v5/manifest.csv`). Manifest rows for
> five tasks carry `excluded-from-training` in `reason` (answer-visible
> through legitimate environment dependencies; the v5 census marks them
> `fail:open-leak:env-dependency`). The purge slot stays `@1` until the
> `@2` lane lands.

## The canonical Python chain

```
ledger run package (repairs)
  -> strip-future-history@1
  -> purge-installed-copies@1   (scoped; see below)
  -> purge-build-caches@4
  -> mtime-normalize@2
  -> separate-verifier@5        (+ solution/solve.sh from the reference fix)
  -> agent-network-none@1       (agent phase only; verifier remains public)
```

| Step | Transform | Closes | Why it is in the chain |
|---|---|---|---|
| 1 | `strip-future-history@1` | V1 future history on branches, V2 unreachable git objects | Rebuilds agent-visible git storage from exactly BASE and its ancestors; without it 67% of code tasks leak the answer through git objects. |
| 2 | `purge-installed-copies@1` | E1 installed/build copies of the fixed project | Removes `build/`, project egg-info and site-packages copies, then reinstalls the base tree editable offline. Scoped to validated targets (below): the block fails closed at setup, and unvalidated projects would break setup instead of leaking. |
| 3 | `purge-build-caches@4` | V5 build/module caches | Retains `@3`'s language-aware and node-build-output purge, while allowing a cache explicitly disabled by its tool (Modal pip cache). Unknown cache failures still fail closed. Pinned to the landed `@4`; historical v2 packages keep `@3`. |
| 4 | `mtime-normalize@2` | V3 fix-bearing mtimes | Touches the worktree to one stamp, warms lazy layers, and retries touch+check so Modal materialization timestamps cannot trip the fail-closed check. Shipped in PR #813; the builder resolves the active mtime generation and records it per manifest row. |
| 5 | `separate-verifier@5` | E2 grader tamper, all runners | Patch-only grading with structured runner checks, tamper gates, config drops, rootdir-robust junit matching, complete pristine-workdir deltas, skip-tolerant grading, and multi-phase junit union (see `docs/mimo/separate-verifier.md`). Runs after all setup-cleaning steps so the verifier inherits the clean setup. |
| 6 | `agent-network-none@1` | V6 upstream fetches | Appends `[agent] network_mode = "no-network"` after verifier derivation. Setup and the separate verifier retain the public baseline; only the agent phase is locked. See the `agent-network-none` receipt and `docs/mimo/vals-routes.md` for backend enforcement. |

## The canonical non-Python chain (all 1,518 tasks)

```
snapshot task dir
  -> strip-future-history@1
  -> purge-build-caches@4
  -> mtime-normalize@2
  -> separate-verifier@5
  -> agent-network-none@1
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
- The probe marker is auto-derived from the hidden test patch
  (`tests/test.patch`): first added `test_*` function name, else the first
  touched file's basename, else the task id, sanitized for the probe hook's
  grep pattern.

## The `verify` column

The manifest's `verify` column is the fleet-census grade per clean
package, fed later by the census lane. Every v3 row ships as
`unverified`; the census updates the field in place (values it defines).
Do not infer package quality from `status=built`: built means the chain
derived, verified means it graded clean.

## How to rebuild

```bash
git fetch origin main
git worktree add ~/Developer/eval-lab/.worktrees/mimo-clean-v3 -b mimo-clean-v3 origin/main
cd ~/Developer/eval-lab/.worktrees/mimo-clean-v3
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
# compare final_digest against research/experiments/mimo-clean-v3/manifest.csv
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
  --tasks <id1,id2,...> --jobs-dir runs/mimo-clean-v3
```

Per task it runs, on local Docker with the task's pinned image: the oracle
control (when a reference fix exists), the nop control, and the full
12-attack cheat ladder, then prints a per-task acceptance row. Acceptance is
**oracle 1, nop 0, cheat clean** (zero cracked trials). Raw jobs stay out of
git; the acceptance table is recorded in
`research/experiments/mimo-clean-v3/README.md`.

## Residual gaps (honest)

- Installed copies outside CONFIRMED_PURGE rely on the HAR-185 sample
  (0/100) plus the vals-routes-v2 census; the fail-closed tasks keep their
  copies by design. If a new installed-copy leak is confirmed anywhere, add
  the task id to `CONFIRMED_PURGE` and rebuild.
- The 000047 `lib/` build-output leak is closed by `purge-build-caches@3`
  (vals-routes-v3 receipt: rebuild via `npm run compile`, oracle 1 / nop 0;
  every v3 package carries `@4` — see the manifest `chain`).
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
