Status: done
Last: merged as PR #76 (`9bbf708`)
Next: none
Blockers: none

# Preflight Handoff (WS-E Item 2)

Implements `evallab preflight` and embeds preflight in the digest:
- `src/evallab/preflight.py`
- `tests/test_preflight.py` (31 tests)
- `src/evallab/cli.py` wiring
- `src/evallab/digest.py` preflight section
- `docs/operations.md`

## Verification
- `uv run pytest tests/test_preflight.py` (31 passed)
- `uv run pytest` (754 passed, 1 xfailed)
- `uv run ruff check .` (clean)
- `uv run evallab preflight` proven live
