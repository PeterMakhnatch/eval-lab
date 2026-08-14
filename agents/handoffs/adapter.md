# ADAPTER handoff

- Role/branch: `ADAPTER` / `role/adapter`
- Worktree: `~/Developer/helab-adapter`
- Owned path: `adapters/` only
- Status: complete and ready for integration

## Delivered

- Self-contained `uv` adapter at `adapters/quixbugs/`, initialized with `harbor adapter init`.
- Source pinned to QuixBugs commit `4257f44b0ff1181dedaedee6a447e133219fcebf`.
- Deterministic full output in `adapters/quixbugs/generated/`: 40 Python tasks.
- Required CLI flags: `--output-dir`, `--limit`, and `--task-ids`; also `--source-dir` and `--overwrite`.
- Hidden separate verifier inputs and Oracle solutions for every task; Harbor transfers only the target source artifact.
- Verifier-owned read-only runners, non-root candidate execution, digest-pinned images, and hash-locked Python packages.
- Staged whole-dataset generation; bounded overwrite and injected-failure regressions cover manifest consistency and non-destructive failure behavior.
- README documents regeneration, controls, parity deferral, limitations, and three canaries.
- Parity remains deferred/model-unset at `$0`; no paid or cloud model run was made.

## Evidence

- Full content digest: `23cebf7f3c641e27afade09d3886dc4de8f55ac72027e900d079b2f49e3789eb`.
- Clean network regeneration was byte-identical (`diff -qr` empty).
- Harbor structural review: 30 passed, 0 errors, 3 expected warnings for null PR links on the deferred parity record.
- Static validation covers 40 Python task contracts, 80 generated shell scripts,
  and compilation of the 40 starting and 40 Oracle Python sources.
- The retained three-task Python subset had 9/9 Oracle rewards of `1.0` and
  3/3 no-op rewards of `0.0`, with no errors.
- The original `pytest.py` bypass regression received reward `0.0` with no error.
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
