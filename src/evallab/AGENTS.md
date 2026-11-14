# Eval Lab core

Read the root change path and 1 domain manual. Package overrides add local rules.

## Boundaries

- `docs/SYSTEM.md` owns module placement and reserved namespaces. Preserve current imports unless the assigned change includes a migration.
- Add CLI routes in the domain's parser registration module, such as `src/evallab/operations_parser.py`, not `cli.py`. Keep dispatch deterministic.
- Put heavy imports inside command callbacks. Contract modules have no runtime database or import side effects.
- `tests/test_cli_startup.py::test_parser_startup_excludes_execution_dependencies` guards parser startup imports.
- Subjective machine judgments are not acceptance. Model execution does not belong in evidence extraction or interpretation.
- Pin the task version, agent/model, Harbor revision, and changed variable for a comparison.
- `sql/schema.sql` and tested parsers own derived contracts. Ingestion is deterministic and idempotent.

## Verification

Use focused consumer tests through the root checkpoint. `agents/CHECKS.md` owns deterministic-test rules and exact-head gates.
Update persisted-contract and CLI documentation when their behavior changes.
Workers skip checks unless the assignment explicitly delegates verification.
