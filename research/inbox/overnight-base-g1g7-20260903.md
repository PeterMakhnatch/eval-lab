---
source_type: internal
---

# Authoritative integration base + G1/G7 verdict (Engineer Lead, 2026-09-03)

## Authoritative head
`origin/integrate/spine-batch1` @ `ccf5567e` (pushed, fetchable). Stack: spine merge `cda75019` (failover tip `b633c47b`) + pairing cherry-pick `dda438eb` (`bcc9f385`) + lane reconciliation `ccf5567e`. All Track A–E work bases here, NOT `origin/main`.

## G1/G7 verdict (measured, not inferred)
- **NOT closed for the legacy corpus:** 0/164 trajectory trial dirs under `research/evidence/runs/` load via `load_trial_bundle`; all raise missing-contract-file. Zero `benchmark_contract.json` files exist under `research/evidence/runs/`.
- The spine ships validators (`load_trial_bundle`, `verify_trial_admissibility`, `parse_benchmark_contract`), NOT retroactive contracts. Promotion-time contract emission is still unbuilt.
- 128-backfill admissibility figure UNVERIFIED on my side. Do not cite.

## Required symbols (all present at the head)
`load_trial_bundle`/`ingest_benchmark_trial`, `verify_trial_admissibility`, `TrialBundle`, `parse_benchmark_contract`, `ContractModel`, `FeatureDefinition.is_quarantined` + `QuarantineReason`, `ZAI_OPENCODE_MODEL_SELECTORS`, `materialize_zai_secret_file(destination, *, home, environment)`, `provider_failover_reason`, `redact_environment`, `collected_secret_values`.

## Lane notes for consumers
- Z.ai models: `zai-coding-plan/glm-5.3` + `.../glm-5.3-flash`; ceilings mandatory; single-trial binding; key sourced from OpenCode `auth.json`.
- Proxy upstream path is now `/api/coding/paas/v4/chat/completions` (`UPSTREAM_PATH`); client gate also accepts `/chat/completions`, `/v1/chat/completions`.
- Reserved by my side: `src/evallab/training_result*`, `tests/test_training_result*`, `approve-all-waiting` CLI, `run_t11_discrimination_gate.py`. Track A takes `training-export*`; Track E takes `paired_intervention*` (verify no collision with `ls` before creating).
