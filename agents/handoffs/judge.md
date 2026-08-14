Status: building
Last: Free nop smoke proved the generated task builds and its verifier completes without exception.
Next: Submit and dispatch the Codex calibration, then collect its artifact into a measured record.
Blockers: Claude credential absent; catalog schema init fails on a pre-existing view; full Ruff has 9 upstream errors.

# JUDGE handoff

Worktree: `.worktrees/judge` on `role/judge`.

Implemented so far:

- `harbor-lab calibrate <family>` modes for deterministic stub, queued judge
  staging, prediction collection, catalog persistence, and DSPy split audit.
- Raw Reward Kit pre-inversion verdict semantics and exact per-criterion agreement.
- Runtime-generated Harbor judge tasks keep all answer keys out of the agent and
  verifier environments.
- Stub results: checkout mean `0.5552`; retry mean `0.5682`; both correctly marked
  non-reportable.
- Checkout DSPy split: 12 train, 4 optimizer-validation, 6 sealed holdouts; overlap
  assertion is active.

No billable call has been made yet. Runtime-only staged tasks/specs live under the
ignored `queue/` tree in this worktree. EVIDENCE's corpus and answer keys are
unchanged.

Verification checkpoint:

- `uv run pytest -q`: 36 passed.
- Owned paths Ruff clean.
- `uv run ruff check .`: blocked only by nine existing violations in
  `library/curated/_emit_card.py` and `research/explorations/harbor-021/demos/`,
  which JUDGE does not own.
- Ephemeral DSPy 3.2.1 `DummyLM`: metric 1.0; spy optimizer saw 16 examples and
  zero of six held-out controls.
- First nop smoke found a missing separate-verifier Dockerfile; generator fixed.
  Second smoke completed one trial with zero exceptions and reward 0 as expected
  for nop. Post-run ingestion still fails in BUILDER-owned catalog DDL with
  `psycopg.errors.InvalidTableDefinition: cannot drop columns from view`.
