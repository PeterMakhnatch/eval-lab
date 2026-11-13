# Operator and CI tooling

Start with [the native operator guide](../README.md). This directory holds the
delivery and evidence tools that guide references. Native application logic
belongs in `src/evallab/`, not a second operator workflow.

## What lives here / entry points
- `scripts/premerge.sh`: Pre-merge validation gate executing local quality checks.
- `scripts/ci/check_harbor_skips.py`: JUnit skip census for native CI. The native file inventory remains until full discovery is safe.
- `scripts/line-replay.py`: Standard frozen `replay-v1` assembly-line regression gate (see `docs/line.md`).

## Invariants or rules
- Platform-owned tooling: `scripts/` belongs to the Platform lane and provides operator and CI automation (cited in `agents/STRUCTURE.md` and `agents/OWNERS.md`).
- Credential safety: Subscription helpers must never alias OAuth tokens to raw API keys or leak credentials (cited in `tests/test_repository_contract.py` and `AGENTS.md`).

## Tests or checks
- `uv run pytest tests/test_quality_gates.py`
- `uv run pytest tests/test_profiles.py`
- `bash scripts/premerge.sh`

The [CI contract](../agents/CHECKS.md) owns current checks and the native discovery
boundary. Existing `scripts/ops/` files are service configuration, not permission
to install, unload, or change a service. Follow the current approval boundary.

## What not to add here
- Do not store one-off ad-hoc shell commands or throwaway analysis scripts here.
- Do not commit credentials, tokens, or private environment files (cited in `AGENTS.md`).
- Do not add core platform or domain logic that belongs in `src/evallab/`.
