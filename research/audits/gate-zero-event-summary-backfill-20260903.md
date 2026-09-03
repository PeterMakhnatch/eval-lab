# Gate Zero backfill audit — canary-event-summary-codex-20260815 — 2026-09-03

Branch: `role/gate-zero-backfill` @ de572342, base `integrate/spine-batch1@6ede71a0`.
Operator: Fable (wS:p9), under Architect (wK:p6) task assignment and ruling.

## What was done (additive-only, digest-guarded)

- Emitted 3 registry-bound `benchmark_contract.json` files
  (`event-summary__5E3btLv`, `event-summary__EKfePmM`, `event-summary__h2D9f6f`)
  via the frozen ContractEmit emitter (`src/evallab/contract_emission.py`:
  `plan_contract_emission` → `atomic_write_bytes`). Emitter and
  `scripts/promote_codex_bundle.py` semantics untouched.
- Whole-tree digest guard: 65 pre-existing paths byte-identical before/after;
  exactly 3 additive files.
- Negative control: symlinked `benchmark_contract.json` destination refuses
  (`ContractEmissionRefusal`), verified by
  `scripts/backfill_benchmark_contracts.py --selftest-symlink`.
- Note: planning must run with `--repo-root` pointing at the checkout whose
  absolute task path the evidence recorded
  (`~/Developer/eval-lab/library/tasks/event-summary`); worktree-relative roots
  refuse with `task_path_mismatch` because `verify_registry_binding`
  (contract_emission.py:267) requires the recorded path inside the repo.
  Task package and registry bytes verified identical across both checkouts.

## Gate Zero status (Architect ruling 2026-09-03, cc wH:p0/wH:p9)

**This family remains `loadable=False`, reason `legacy_pre_bundle_corpus`,
`admissible=False` — permanently for Gate Zero purposes.**

`load_trial_bundle` (`src/evallab/interpretation/benchmark_events.py:1291`)
refuses all three trials with typed errors:

- `BenchmarkMissingArtifactError: Benchmark events file not found in: …`
  (requires one of `_NESTED_EVENTS_CANDIDATES`, benchmark_events.py:327-334)
- and would next refuse `Benchmark events`/final-state
  (`_NESTED_FINAL_STATE_CANDIDATES`, benchmark_events.py:335-340) — no trial in
  any event-summary family (canary 20260815, oracle, nop) carries a native
  benchmark-events stream or final-state artifact; the corpus predates the
  bundle format.

Per ruling: no derivation path synthesizing `benchmark-events`/`final-state`
from ATIF `agent/trajectory.json` or `result.json` `step_results` may be emitted
under native artifact names (d709cf6d defect class — authority minted from
reconstruction). `load_trial_bundle` must not be extended to accept derived
streams. A separately-typed `derived-events/v1` analysis-only sidecar (source
digests + transform id/version + `derived=true`, barred from G1/G7 and A–D
admission) is the permitted follow-up; training-side use of this family requires
re-promotion from `runs/` as a fresh native bundle.

## Verification transcript

- `uv run python scripts/backfill_benchmark_contracts.py --selftest-symlink`
  → `symlink negative: OK`
- `--apply` → 3 contracts emitted, digest guard `65 pre-existing paths
  unchanged, 3 additive file(s)`
- `--verify` → 3 × `FAIL … load_trial_bundle: Benchmark events file not found`
  (expected; typed refusal recorded above)
- `uv run pytest -q tests/test_promotion_contract_emission.py tests/test_trial_admissibility.py`
  → 44 passed
