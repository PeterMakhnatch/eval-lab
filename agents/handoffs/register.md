Status: done
Last: repaired REGISTER admission trust boundary (contracts, evidence validation, canonical resolution, preflight, audit strictness, acceptance tests)
Next: ready for review and integrator reconciliation
Blockers: agents/ROLES.md row conflict with PROGRAM role (recorded for integrator)

## Accomplishments

1. **Promoted Control Evidence Contract & Verification (`src/evallab/schemas.py`, `src/evallab/registry.py`)**:
   - `ControlEvidenceRef` models `evidence_path` (repo-relative), `evidence_digest` (`sha256:` prefix), and UTC `observed_at`.
   - `verify_control_evidence` parses and checks on-disk evidence JSON files (e.g. `research/evidence/runs/event-summary-oracle-evidence/result.json`), validating exact `reward == 1.0` for Oracle and `reward == 0.0` for Nop, as well as task and agent identity.
   - Missing evidence files, unpromoted runs, tampered digests, or non-matching rewards fail closed immediately.
   - Downgraded `transaction-reconciliation` and `terminal-bench-html-js-filter` candidate claims until durable promoted evidence files are executed and placed in `research/evidence/runs/`.

2. **External Records, Package Completeness, & Source Immutability (`src/evallab/registry.py`)**:
   - For `provenance_zone == "01-external"`, non-empty `license` and immutable pinned `source_ref` (commit SHA or release tag) are strictly required. Floating refs (`latest`, `head`, `main`, `master`) are rejected.
   - `verify_package_completeness` ensures registered packages contain `task.toml`, `instruction.md` / `instructions.md`, `environment/` or `Dockerfile`, and separate `tests/` / `verifier/`.

3. **Canonical Spec Resolution & Enforced Limits (`src/evallab/registry.py`, `src/evallab/queue.py`)**:
   - `TaskRegistry.resolve_spec` resolves `registered/<task_id>` into canonical `task_path`, `task_version`, verifier digest, package digest, and timeout limits.
   - Omitted `spec.task_path` automatically resolves to the canonical `record.task_path`.
   - `Executor.execute_spec` binds canonical resolved paths and digests to `RunRequest` and persists them into `RunProvenance(task_digest=..., verifier_digest=..., task_path=...)`, preventing untrusted spec overrides.
   - Enforced limits clamp `spec.timeout_seconds` to `record.limits.timeout_seconds`.

4. **Allowed Uses & Researcher Loop Preflight (`src/evallab/researchers.py`)**:
   - Enforced `allowed_uses`: training-only tasks (`allowed_uses: ["training"]`) cannot back measurement experiments.
   - `ResearcherLoop.run()` preflights `TaskRegistry.from_repo()` before any model invocation. If the registry is empty or contains zero measurement-eligible tasks, the loop defers immediately with `no_registered_tasks` without making any LLM calls or incurring token costs.

5. **Registry Audit Strictness (`src/evallab/registry.py`)**:
   - `audit_registry` reports malformed JSON in queue specs (`malformed_queue_spec`) and invalid registry records (`malformed_registry_record`) without swallowing parse errors.
   - Audits package component existence, disk digests, control evidence files and rewards, external licenses, and queue claims (`false_registered_claim`, `task_path_redirection`, `task_version_mismatch`, `verifier_digest_mismatch`).

6. **Acceptance Test Suite (`tests/test_registry.py`)**:
   - 22 unit, end-to-end, and adversarial tests covering: candidate refusal, missing task refusal, tampered task bytes refusal, verifier change refusal, task redirection refusal, version mismatch refusal, omitted path resolution, control evidence missing/tampered/wrong reward refusal, training-only refusal, missing component refusal, PolicyGate human-approval override inability to bypass, Executor.tick end-to-end dispatch, canonical RunProvenance persistence, ResearcherLoop preflight zero model calls, independent canary suite integrity, audit malformed spec detection, and schema strictness.

---

## Validation & Verification Records

### Three Consecutive Local Acceptance Passes
1. **Pass 1**: `scripts/premerge.sh` (Python 3.12, 240 pytest passes in 10.52s, Docker-free smoke PASS, ty 28 <= 28, ruff clean).
2. **Pass 2**: `scripts/premerge.sh` (Python 3.12, 240 pytest passes in 10.58s, Docker-free smoke PASS, ty 28 <= 28, ruff clean).
3. **Pass 3**: `scripts/premerge.sh` (Python 3.12, 240 pytest passes in 10.52s, Docker-free smoke PASS, ty 28 <= 28, ruff clean).

### Clean Detached In-Repo Worktree Verification
- Worktree created at `.worktrees/test-detached` from HEAD (`9290a19`).
- Ran `uv sync --locked`, `uv run ruff check .`, `uv run pytest tests/test_registry.py` (15 passed in 4.56s).
- Worktree removed cleanly.

---

## Integration Blocker Note for Integrator

- **File**: `agents/ROLES.md`
- **Context**: Branch `role/register` (PR #36) was created before `PROGRAM`'s row was finalized on `origin/main`. In accordance with AGENTS.md rules ("never edit another role's row or main"), `REGISTER` has not edited or overwritten `PROGRAM`'s row.
- **Action for Integrator**: When merging PR #36 into `main`, retain both the `PROGRAM` row and the `REGISTER` row in `agents/ROLES.md`:
  ```markdown
  | PROGRAM | `role/program` | `research/experiments/`, `agents/handoffs/program.md` | Reconcile the experiment journal into a truthful ledger and next-experiment agenda | Done: six studies reconciled; 2026-08-15 Codex canaries recorded; three unsubmitted drafts. |
  | REGISTER | `role/register` | `src/evallab/registry.py`, `tests/test_registry.py`, `library/registry/`, `research/registration/`, `docs/task-registry.md`, `agents/handoffs/register.md` | Explicit task admission, canonical registry contract, audit CLI, task inventory | Done: admission trust boundary, evidence verification, canonical resolution, preflight, audit, and acceptance tests green. |
  ```
