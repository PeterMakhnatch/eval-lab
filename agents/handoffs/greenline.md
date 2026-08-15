Status: review-wanted
Last: 263 collected and passing; ruff/ty/smoke green; REGISTER #36 blocks merge
Next: premerge x3 + clean-worktree premerge, then push PR
Blockers: REGISTER PR #36 is open; do not self-merge GREENLINE

Worktree already existed at start. Rebased/used `.worktrees/greenline` on
`role/greenline` at `078dd7b` (origin/main PROGRAM merge). Did not delete it.

## Collection

Default `uv run pytest --collect-only -q -o addopts=` reports **263 tests**.
Floor was 257 (229 `tests/` + 8 dashboard + 25 analysis + 9 calibration).
New contract tests added 6 (2 collection + 4 PROGRAM).

Collected suites:

- `tests/` (221, including the two new modules)
- `dashboard/tests/` (8)
- `research/analysis/tests/` (25)
- `research/calibration/tests/` (9)

Harbor task verifiers under `library/**/tests/` stay out of default
collection. No `tests/live/` or `tests/integration/` exists.

`tests/test_ci_coverage.py` fails if a committed `test_*.py` under the
declared dirs is omitted. Negative path is
`test_collection_contract_detects_an_omitted_module`.

## Fixtures

`research/analysis/tests/test_atif.py` `_make_job` now writes
`started_at`/`finished_at` on job and trial `result.json`.
`src/evallab/results.py` `load_job` is unchanged and still raises
`ValueError("Not a completed Harbor job directory")` without `finished_at`
(`tests/test_program_contract.py::test_load_job_still_rejects_a_job_missing_finished_at`).

Artifact-count expectation kept and aligned: oracle 3 artifacts / 1 missing,
nop 3 artifacts / 2 missing (manifest entries, including missing paths).

## PROGRAM contract

`tests/test_program_contract.py` imports
`research/experiments/validate_program.py` via `importlib` and calls
`validate()`. Covers committed `PROGRAM.json`, schema_version/empty
rejection, and a missing-required-keys experiment.

## Local checks (pre-premerge)

- `uv run pytest`: 263 passed in 10.99s
- `uv run ruff check .`: All checks passed
- `uvx ty@0.0.71 check src/`: Found 28 diagnostics (baseline 28)
- `uv run python -m evallab.smoke --docker-free`: SMOKE PASS
- load_job missing `finished_at`: still raises

Scratch: `/var/folders/zv/j4ds5l7j01ldjyw0t0yzcv8w0000gn/T/grok-goal-9d250547c72f/implementer/`
