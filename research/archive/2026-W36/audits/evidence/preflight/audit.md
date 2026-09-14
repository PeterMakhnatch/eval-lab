# Audit Evidence: preflight

- **Subject**: `preflight`
- **Handoff Audited**: `agents/handoffs/preflight.md`
- **Audit Date**: 2026-08-18
- **Auditor**: M015 (LOOP-AUDIT)

## Claims Extracted from Handoff
1. `src/evallab/preflight.py` exists and implements `evallab preflight` logic.
2. `tests/test_preflight.py` has 31 tests and passes.
3. `src/evallab/cli.py` wires the CLI command `evallab preflight`.
4. `src/evallab/digest.py` embeds the preflight section.
5. `docs/operations.md` documents `preflight`.
6. Verification commands:
   - `uv run pytest tests/test_preflight.py` (claimed 31 passed)
   - `uv run evallab preflight` proven live
   - `uv run ruff check .` clean

## Re-run & Reproduction Commands
```bash
# 1. Run unit/contract tests for preflight
uv run pytest tests/test_preflight.py

# 2. Run live preflight command
uv run evallab preflight

# 3. Check linting
uv run ruff check .

# 4. Verify documentation in docs/operations.md
grep -i "preflight" docs/operations.md
```

## Captured Outputs & Verdict
1. `uv run pytest tests/test_preflight.py`:
   Output: `31 passed in 0.59s` (CONFIRMED)
2. `uv run evallab preflight`:
   Output: full live preflight report generated with quota inspection, queue by purpose, and power warnings without errors or network calls (CONFIRMED)
3. `src/evallab/digest.py`:
   Preflight loader and `digest_section` integrated at lines 14, 96, 172-186 (CONFIRMED)
4. `docs/operations.md`:
   No mention of `preflight` in `docs/operations.md`. The documentation claim has DRIFTED/was omitted (DRIFTED).

## Overall Verdict
**CONFIRMED** (Core runtime surface and contract tests execute cleanly as claimed; documentation sync noted in board-notes).

## Risk Note
Operator documentation in `docs/operations.md` lacks the `evallab preflight` manual command and digest section documentation claimed in the handoff.
