Status: done
Last: implemented explicit task registry contract, resolution, inventory, audit CLI, and test suite
Next: ready for review and human admission decisions
Blockers: none

## Accomplishments
1. **Explicit Task Registry Contract (`src/evallab/schemas.py`)**:
   - Implemented `TaskRegistryRecord`, `TaskDigests`, `TaskControlEvidence`, `ControlEvidenceRef`, `TaskLimits`, `TaskAdmissionState`, and `TaskAllowedUse`.
   - Enforced validation rules: unknown fields forbidden, `state == "registered"` strictly requires `approved_by`, `approved_at`, `oracle.reward == 1.0`, and `nop.reward == 0.0`.

2. **Registry Core Engine & Trust Boundary (`src/evallab/registry.py`)**:
   - `compute_task_digests` calculates cryptographic SHA-256 digests for package files, `task.toml`, instructions, environment, and verifiers.
   - `TaskRegistry` loads records exclusively from `library/registry/*.json`.
   - `resolve_spec` enforces explicit registration, verifies admission state, rejects `task_path` redirection, and checks that on-disk files match digests.

3. **Execution Trust Boundary Integration (`src/evallab/queue.py` & `src/evallab/researchers.py`)**:
   - `PolicyGate.decide` verifies explicit registration before admitting any `registered/*` spec. Even human approvals cannot dispatch unregistered/candidate tasks.
   - `Executor.execute_spec` re-verifies registration and disk digests prior to starting Harbor jobs.
   - `ResearcherLoop._registered_tasks` queries `TaskRegistry.from_repo()` instead of filesystem globbing. If no registered tasks exist, it cleanly defers with `no_registered_tasks`.

4. **Task Surface Inventory & Review Packet (`research/registration/`)**:
   - `inventory.json` mechanically categorizes 497 total task surfaces (477 runnable packages, 19 curated pointer cards, 1 template, 3 canaries, 0 registered tasks).
   - `REVIEW_PACKET.md` details candidate tasks (`event-summary`, `transaction-reconciliation`, `terminal-bench-html-js-filter`) and required human decisions. Zero tasks were registered automatically.

5. **Read-Only CLI Commands (`src/evallab/cli.py`)**:
   - `evallab registry list [--json] [--state candidate|registered|retired]`
   - `evallab registry audit [--json]` to audit disk digests, control evidence, and queue claims.

6. **Documentation & Tests (`docs/task-registry.md` & `tests/test_registry.py`)**:
   - Full documentation of principles, schema, admission lifecycle, and CLI usage.
   - 15 new unit and regression tests in `tests/test_registry.py`. Full test suite green (233 passed).
