# Operator and CI tooling

## Purpose
`scripts/` houses automation utilities for operators, continuous integration, and
fleet maintenance. It provides hooks, verification tools, profiling harnesses,
and promotion helpers.

## What lives here / entry points
- `scripts/premerge.sh`: Pre-merge validation gate executing local quality checks.
- `scripts/ci/check_harbor_skips.py`: JUnit skip-reason summary and Harbor-named skip gate for native CI.
- `scripts/ci/report_slowest_files.py`: Per-file summed durations of the 20 slowest test files from a JUnit report, printed per CI test shard.
- `scripts/fleet-status.sh`: Fleet tracking and active status report script.
- `scripts/line-replay.py`: Standard frozen `replay-v1` assembly-line regression gate (see `docs/line.md`).
- `scripts/setup-git.sh`: Local git hooks and repository configuration helper.
- `scripts/promote_codex_bundle.py`: Sanitization and promotion utility for reviewed run evidence.
- `scripts/profile/harness.py`: Performance profiling harness and latency budget check.
- `scripts/review_pr.py`: Advisory independent PR review (shallow or parallel deep lanes), fix verification, and served-model attestation ([usage](../docs/independent-review.md)).
- `scripts/ops/`: System service units and continuous-operator daemon configurations.

## Invariants or rules
- Platform-owned tooling: `scripts/` belongs to the Platform lane and provides operator and CI automation (cited in `agents/STRUCTURE.md` and `agents/OWNERS.md`).
- Credential safety: Subscription helpers must never alias OAuth tokens to raw API keys or leak credentials (cited in `tests/test_repository_contract.py` and `AGENTS.md`).
- Promotion sanitization: Evidence promotion scripts must redact secret keys and prompts before committing bundles (cited in `tests/test_promotion_opencode_r2.py` and `AGENTS.md`).

## Tests or checks
- `uv run pytest tests/test_quality_gates.py`
- `uv run pytest tests/test_profile_harness.py`
- `uv run pytest tests/test_promotion_opencode_r2.py`
- `uv run pytest tests/test_independent_review_contract.py tests/test_review_pr.py`
- `bash scripts/premerge.sh`

## What not to add here
- Do not store one-off ad-hoc shell commands or throwaway analysis scripts here.
- Do not commit credentials, tokens, or private environment files (cited in `AGENTS.md`).
- Do not add core platform or domain logic that belongs in `src/evallab/`.
