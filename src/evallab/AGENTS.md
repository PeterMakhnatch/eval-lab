# Eval Lab core

Read the root change path and 1 domain manual. Package overrides add local rules.

## Boundaries

- `docs/SYSTEM.md` owns module placement and reserved namespaces. Preserve current imports unless the assigned change includes a migration.
- Keep CLI registration and dispatch deterministic. Contract modules have no runtime database or import side effects.
- Subjective machine judgments are not acceptance. Model execution does not belong in evidence extraction or interpretation.
- Pin the task version, agent/model, Harbor revision, and changed variable for a comparison.
- `sql/schema.sql` and tested parsers own derived contracts. Ingestion is deterministic and idempotent.

## Verification

Use focused consumer tests through the root checkpoint. `agents/CHECKS.md` owns deterministic-test rules and exact-head gates.
Update persisted-contract and CLI documentation when their behavior changes.
Workers skip checks unless the assignment explicitly delegates verification.
