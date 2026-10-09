---
status: living
audience:
  - builder
  - analyst
  - runner
---

# mimo-clean-v1 clean set

The canonical clean task set for the MiMo Python code pool: one derived
package per task the Python task ledger marks usable (`keep`/`fix`;
`discard` rows are skipped with reason), each carrying the full clean chain
in its lineage.

- Version id: `mimo-clean-v1`
- Manifest (tracked, small): `research/experiments/mimo-clean-v1/manifest.csv`
- Builder: `src/evallab/mimo_clean.py`
- CLI: `evallab mimo-clean build|verify-local`
- Acceptance receipt: `research/experiments/mimo-clean-v1/README.md`

## The canonical Python chain

```
ledger run package (repairs)
  -> strip-future-history@1
  -> purge-installed-copies@1   (scoped; see below)
  -> purge-build-caches@1
  -> mtime-normalize@1
  -> separate-verifier@2        (last; + solution/solve.sh from the reference fix)
```

| Step | Transform | Closes | Why it is in the chain |
|---|---|---|---|
| 1 | `strip-future-history@1` | V1 future history on branches, V2 unreachable git objects | Rebuilds agent-visible git storage from exactly BASE and its ancestors; without it 67% of code tasks leak the answer through git objects. |
| 2 | `purge-installed-copies@1` | E1 installed/build copies of the fixed project | Removes `build/`, project egg-info and site-packages copies, then reinstalls the base tree editable offline. Scoped to validated targets (below): the block fails closed at setup, and unvalidated projects would break setup instead of leaking. |
| 3 | `purge-build-caches@1` | V5 build/module caches | Removes regenerable caches (`__pycache__`, pytest/mypy/ruff caches, node `.cache`) that can carry the fixed tree. Dependency dirs stay (offline graders need them). |
| 4 | `mtime-normalize@1` | V3 file mtimes pointing at fixed files | Touches the worktree to one stamp so `find -newermt` cannot rank the fixed files. |
| 5 | `separate-verifier@2` | E2 grader tamper (conftest, sitecustomize, PATH shadow, reward writer, in-source exit, pytest monkeypatch) | Patch-only grading: the verifier reruns the bundled clean setup itself, diffs the agent's repo-file patch with its own git, drops test-infra paths, gates tamper signatures, applies the hidden tests and grades with a structured junit check. It is last because it bundles the parent's clean setup chain into the verifier image. |

The builder reuses the existing `derive_*` functions in order
(`evallab.strip_future_history`, `purge_installed_copies`,
`purge_build_caches`, `mtime_normalize`, `separate_verifier`) and records
each step's lineage via `evallab.task_variants.derive_task`. Steps the run
package already carries (repair rows ship strip/purge) are kept, not
re-derived.

## Purge scoping (read this before widening)

`purge-installed-copies@1` applies to exactly two groups:

1. **CONFIRMED_PURGE** (`format-code-task-001269`, `format-code-task-002308`):
   installed-copy leaks confirmed by reading the image. Both run packages
   already carry the marker, so the chain keeps it.
2. **Already-carried run packages**: repair rows whose setup already applies
   the marker (validated by the repair lane).

Everything else skips with reason:

- **Fail-closed** (`exploit_probe.PURGE_INAPPLICABLE`, HAR-194, reproduced in
  local Docker with setup rc=1): 002552 (poetry on py3.8, offline reinstall
  cannot use its backend), 000792/002486 (no identifiable project name),
  002139 (bitbake pip-installs a real copy), 002391 (PEP 668 refuses pip).
- **Out of scope**: all remaining tasks. HAR-185 confirmed 0 of 100 sampled
  images outside the two confirmed targets, and the HAR-194 stance is that
  purge applies only where validated — the block fails setup closed rather
  than leaking, so blind application would trade unproven leaks for certain
  setup breakage.

## Reference fixes and markers

- The manifest's `reference_fix` comes from
  `research/experiments/python-task-ledger/oracle_sweep.csv` rows labeled
  `oracle:pass*` whose HAR-191 solution patch file exists
  (`.../tasks/<task>/oracle/solution-patch.stdout.log`). 192 of 1148 built
  tasks have one; the rest record `none`.
- `solution/solve.sh` embeds that patch base64 and applies it with
  `git apply` at the agent worktree repo root, so `evallab run --agent
  oracle` grades 1. It is added only when the parent has no solution
  (overwrite refused by the transform).
- The `@2` probe marker is auto-derived from the hidden test patch
  (`tests/test.patch`): first added `test_*` function name, else the first
  touched file's basename, else the task id, sanitized for the probe hook's
  grep pattern.

## How to rebuild

```bash
git fetch origin main
git worktree add ~/Developer/eval-lab/.worktrees/mimo-clean -b mimo-clean-v1 origin/main
cd ~/Developer/eval-lab/.worktrees/mimo-clean
uv sync --frozen --extra laminar
uv run --extra laminar evallab mimo-clean build
```

The build is deterministic and idempotent: every step reuses the existing
lineage record for `(task, transform, parent digest)` and derives
(content-addressed) only what is missing, so re-running yields the same
`final_digest` values and never duplicates records. Verify a rebuild with:

```bash
uv run evallab mimo-clean build --tasks <id1,id2,...>
# compare final_digest against research/experiments/mimo-clean-v1/manifest.csv
```

A single task (or subset) rebuilds with `--tasks a,b --workers N`.

## Lineage records: why only the manifest is committed

The build wrote 3,440 new lineage JSONs (~72 MB: each record inlines the
changed setup/task.toml/test files) into `library/task-variants/`. The
tracked tree already holds ~15k records (~226 MB). Committing another
~72 MB in one PR is not reviewable and duplicates bytes the builder
reproduces deterministically, so this slice commits only the manifest plus
the reproducible build command above. The manifest pins every
`final_digest`, so any rebuild is verifiable byte-for-byte. (Rule used:
commit lineage iff the new total stays under ~20 MB.)

## How to verify

```bash
uv run --extra laminar evallab mimo-clean verify-local \
  --tasks <id1,id2,...> --jobs-dir runs/mimo-clean-v1
```

Per task it runs, on local Docker with the task's pinned image: the oracle
control (when a reference fix exists), the nop control, and the full
12-attack cheat ladder, then prints a per-task acceptance row. Acceptance is
**oracle 1, nop 0, cheat clean** (zero cracked trials). Raw jobs stay out of
git; the acceptance table is recorded in
`research/experiments/mimo-clean-v1/README.md`.

## Non-Python code tasks

The ledger pool behind this manifest is all Python (`format-code-task-*`),
so every row has `domain=code, language=python`. Non-Python tasks plug in
via `LANGUAGE_CHAINS` in `src/evallab/mimo_clean.py` once
`separate-verifier@2` is ported past Python/pytest (partial Go support
exists: `*_test.go`/TestMain drops, exit-code fallback for non-pytest
commands). Until then unknown languages resolve to no chain and are skipped
with reason `no clean chain for language ...`. The non-Python chain is
otherwise `snapshot -> strip -> (language cache purge) -> mtime -> @2`.

## Residual gaps (honest)

- Installed copies outside CONFIRMED_PURGE rely on the HAR-185 sample
  (0/100); the fail-closed five keep their copies by design. If a new
  installed-copy leak is confirmed anywhere, add the task id to
  `CONFIRMED_PURGE` and rebuild.
- `@2` keeps the documented residual from the separate-verifier-v2 receipt:
  an import-time kill that first prints non-marker junk (non-blank,
  markerless output, rc 0, no junit) still takes the exit-code fallback.
- Tasks without a reference fix (956/1148) skip the oracle cell; their
  acceptance is nop 0 + cheat clean, and solvability still rests on the
  census evidence cited in the ledger.
