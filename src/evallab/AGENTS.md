# Eval Lab core

The package owns CLI dispatch, policy-controlled execution, immutable evidence ingestion, storage projections, interpretation, and recovery. Preserve existing module/import locations unless the assigned change includes a migration and updates every caller.

## Boundaries

- Shared Pydantic contracts: `schemas/`. Storage paths/attachments/compaction: `storage/`. Deterministic extraction: `evidence/`. Bounded evidence packs and judgments: `interpretation/`. Recovery bundles/certification: `recovery/`.
- `cli/` and `execution/` are currently reserved namespaces. CLI remains `cli.py`; execution remains the existing top-level runner, queue, quota, contracts, and preflight modules. Do not create parallel implementations because a reserved folder exists.
- `trajectory_ir.py` and `interpretation/trajectory_ir.py` have different contracts. Keep both unless the assigned migration explicitly covers their semantics and persisted consumers; no facade re-export or incidental deletion.
- Subjective machine judgments are not acceptance. Model execution does not belong in evidence extraction or interpretation.
- Keep CLI registration and dispatch deterministic. Do not add runtime DB/import side effects to contract modules.

## Where to look

Use existing modules for the requested surface: `cli.py`, `runner.py`, `queue.py`, `quota.py`, `execution_contracts.py`, `preflight.py`, `evidence_store.py`, `state_events.py`, `trajectory_ir.py`, `interpretation/feature_registry.py`, and the relevant package. The generated `docs/repo-map.md` is lookup, not mandatory reading.

## Verification

Update tests and persisted/CLI documentation for the touched contract. The assigned verifier runs focused checks through the repo environment (`uv run pytest <affected-tests>`); full matrices and merge gates are defined once in `agents/CHECKS.md`. Workers skip checks unless the assignment explicitly delegates verification.
