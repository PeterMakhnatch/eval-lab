Status: done
Last: merged as PR #83 (`723f34d`)
Next: none
Blockers: none

# BUILDER authoring pipeline handoff (WS-C)

Implements `docs/build-plan.md` WS-C in the leased worktree
`.worktrees/builder-pipeline` on `role/builder-pipeline`.

Files:
- `src/evallab/authoring.py`
- `tests/test_authoring.py`
- `docs/authoring.md`
- `agents/handoffs/builder-pipeline.md`

`cli.py` is not wired (`python -m evallab.authoring` only), matching craft.

## Behaviour
- State machine: `proposed → battery_passed → craft_reviewed`; `registered`
  is refused (`RegisterRefusal`). Ledger upsert also refuses that outcome.
- Quarantine: `library/tasks/_proposed/<proposal_id>/`.
- Ledger: `derived/parquet/qualification/ledger.parquet` with the specified
  columns. Pass-rate per `seed_class` is `SEED_CLASS_PASS_RATE_SQL`.
- Seeds: mutation (new version, source digest-checked), scenario
  (`research/`), craft-gap (first uncovered CRAFT facet triple).
- Battery: oracle / nop (n=2) / fair-oracle / adversarial via a free
  structural runner. No paid models, no Harbor in the default path.

## Verification
- `uv run pytest tests/test_authoring.py` — 15 passed
- `uv run pytest` — 987 passed, 1 xfailed
- `uv run ruff check .` — clean
- `python -m evallab.authoring batch --count 5` — five proposals reached
  `craft_reviewed` and printed `REGISTER_REFUSAL`; no ledger row is
  `registered`
