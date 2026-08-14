# ADAPTER handoff

- Role/branch: `ADAPTER` / `role/adapter`
- Worktree: `~/Developer/helab-adapter`
- Owned path: `adapters/` only
- Status: complete and ready for integration

## Delivered

- Self-contained `uv` adapter at `adapters/quixbugs/`, initialized with `harbor adapter init`.
- Source pinned to QuixBugs commit `4257f44b0ff1181dedaedee6a447e133219fcebf`.
- Deterministic full output in `adapters/quixbugs/generated/`: 80 tasks (40 Python + 40 Java).
- Required CLI flags: `--output-dir`, `--limit`, and `--task-ids`; also `--language`, `--source-dir`, and `--overwrite`.
- Hidden separate verifier inputs and Oracle solutions for every task; Harbor transfers only the target source artifact.
- Verifier-owned read-only runners/build files, non-root candidate execution, digest-pinned images, hash-locked Python packages, and checksum-verified offline Java tests.
- Staged whole-dataset generation; bounded overwrite and injected-failure regressions cover manifest consistency and non-destructive failure behavior.
- README documents regeneration, controls, parity deferral, limitations, and three canaries.
- Parity remains deferred/model-unset at `$0`; no paid or cloud model run was made.

## Evidence

- Full content digest: `894c6cfd59cc5513c75fa758a0092420a8b24befdeedc5fb46a9f91b8dc60445`.
- Clean network regeneration was byte-identical (`diff -qr` empty).
- Harbor structural review: 30 passed, 0 errors, 3 expected warnings for null PR links on the deferred parity record.
- Static validation: Ruff passed; all 80 TOML files parsed; all 160 generated shell scripts passed `bash -n`; all 40 starting and 40 Oracle sources compiled in each language.
- Five-task Oracle sample with `k=3`, concurrency `2`: 15/15 reward `1.0`, 0 errors.
- Same five tasks with nop, `k=1`, concurrency `2`: 5/5 reward `0.0`, 0 errors.
- Original `pytest.py` and mutable-`build.gradle` bypass regressions: 2/2 reward `0.0`, 0 errors.
- Structured record: `adapters/quixbugs/verification_evidence.json`.
- Raw ignored jobs:
  - `runs/quixbugs-adapter/separate-oracle-final/2026-08-13__22-09-37/`
  - `runs/quixbugs-adapter/separate-nop-final/2026-08-13__22-11-13/`
  - `runs/quixbugs-adapter/bypass-regression-final/2026-08-13__22-09-13/`

## Validation commands

Run from the worktree root unless a command changes directory:

```bash
uv run --frozen ruff check adapters/quixbugs/src

cd adapters/quixbugs
uv sync --frozen
rm -rf /tmp/quixbugs-clean-regeneration
uv run quixbugs --output-dir /tmp/quixbugs-clean-regeneration
diff -qr generated /tmp/quixbugs-clean-regeneration
```

The exact five-task Harbor commands are in `adapters/quixbugs/README.md`.

## Next step

Review the scoped commit and integrate `role/adapter` when desired. Billable parity remains a later experiment after the lab queue exists and a matching original-system protocol is fixed.

## Blockers

None. The structural review's three warnings are intentionally null adapter/dataset/parity PR URLs; no PR exists yet, and fabricating links would be incorrect.

## Process-document note

The review referenced `docs/parallel-work.md`, but that file does not exist in this branch/worktree. ADAPTER therefore applied the available root `AGENTS.md` and `docs/architecture.md` rules and preserved the assigned `adapters/`-only boundary. If `docs/parallel-work.md` is required for integration, the coordinating branch should restore or add it; creating a root documentation file is outside ADAPTER ownership.

## Coordination

No files outside `adapters/` were intentionally modified. Root `pyproject.toml` and `uv.lock` were not edited. Before integration, inspect `git status`, rerun the structural checks, and commit only `adapters/` paths.
