Status: review-wanted
Last: PR #3 opened; nested ruff.toml silences vendored Hub sources
Next: wait CI; squash-merge only if quality green on our paths
Blockers: quality CI red on main already (curated/explorations ruff); not ours

INGEST 2026-08-14 (`.worktrees/ingest`, `role/ingest`).
Owned: `library/benchmarks/`, this file; protocol also allows `agents/ROLES.md` row.

## Acceptance

- SURVEY.md: 16 candidates (required 12) + rejected table.
- Materialized (Hub, never `@latest`):
  - `aime@1.0` (60) pin `414014c23ce4d32128073d12b057252c918cccf4`
  - `gpqa-diamond@1.0` (198) pin `1983ac5c4d43f43cb7a9af9f89c54d09025589ec`
  - `humanevalfix@1.0` (164) pin `ab02ff13250fae8d91b93a6e4c11ce0bdcb78215`
  - `terminal-bench-sample@2.0` (10) pin `7e917f35c281188532772312d4ad91ca9274febc`
- Sample verification (Harbor 0.21.0, `-k 1 -n 2`, jobs in this worktree `./runs/`):

| Bench | Tasks | Oracle | Nop |
| --- | --- | --- | --- |
| AIME | aime_60–64 | 1.0 | 0.0 |
| GPQA-Diamond | 0,1,10,100,101 | 1.0 | 0.0 |
| HumanEvalFix | python-0,1,10,100,101 | 1.0 | 0.0 |
| TB-sample | regex-log, log-summary-date-ranges, chess-best-move, fix-code-vulnerability | 1.0 | 0.0 |

Skipped: qemu-* (VM), polyglot-c-py / build-cython-ext (non-Python imported), SWE/OSWorld/MLE/GAIA/HLE (survey reasons). No GPU, no cloud, no billable models.

Canary vs experiment nominations: `library/benchmarks/README.md`.

## Continuation (not this PR)

- `bfcl_parity@1.0` (123) or `livecodebench@6.0` next slice.
- Do not vendor full SWE-bench images.

## Note for BUILDER

`library/benchmarks/` is not yet in `agents/STRUCTURE.md` map. INGEST cannot edit STRUCTURE.
