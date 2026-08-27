---
status: historical
audience:
  - builder
---

> **Archived work order**: Completed historical work-order. Living contracts: docs/analysis-loop.md, docs/operations.md. Board: agents/missions/ACTIVE.md.

# Build experiment proposals and the execution approval gate

## Mission

Turn accepted findings into versioned, reviewable experiment proposals without
creating an uncontrolled self-triggering agent loop. The system may prepare the
next experiment; execution remains a separate policy decision.

Work on a named branch/worktree after structured analysis and cohort comparison
are complete. Read `AGENTS.md`, `docs/architecture.md`,
`docs/analysis-loop.md`, and current runner safety checks first.

## Required behavior

1. Define a versioned proposal schema containing:
   - source finding/review IDs and digests;
   - hypothesis;
   - competing explanations;
   - one primary variable to change;
   - variables that must remain fixed;
   - task, verifier, agent/model/environment selections;
   - attempts, concurrency, timeout, and maximum expected cost;
   - controls and validity checks;
   - predicted observations under each explanation;
   - stop conditions and approval class.
2. Generate a draft proposal from accepted findings using deterministic
   templates first. An optional proposal agent may improve wording or reasoning
   only behind the existing model-call approval boundary.
3. Validate that referenced task and experiment definitions exist and that their
   digests match. Detect duplicate proposals using source and configuration
   digests.
4. Implement explicit states:

   ```text
   draft -> validated -> awaiting_approval -> queued -> running
         -> completed | failed | cancelled
   ```

5. Do not encode arbitrary shell commands in a proposal. Compile a validated
   proposal into the existing typed `RunRequest`/matrix path.
6. Permit policy-approved local `oracle` and `nop` controls only when task,
   names, destinations, attempts, and concurrency are explicit and bounded.
7. Require a human approval record for real models, model judges, cloud
   environments, large sweeps, deployments, and publications. Approval records
   identify the exact immutable proposal digest; changing the proposal
   invalidates approval.
8. Use idempotency keys so retries cannot silently create duplicate paid runs.
   Distinguish infrastructure retry from an additional stochastic attempt.
9. Record the produced Harbor job path/ID and final status without editing the
   proposal or source finding.
10. Provide plan, validate, approve-record, run, cancel, and status CLI behavior
    as appropriate. `run` must fail closed if approval is absent or stale.

## Tests

- valid local Oracle/no-op proposal;
- proposal changing more than one declared primary variable;
- missing or stale source digests;
- duplicate idempotency key;
- real-model and cloud proposals blocked without approval;
- approval invalidated by any proposal change;
- attempt retry semantics kept distinct from infrastructure retry;
- no arbitrary command execution from proposal fields;
- completed run linked to its immutable proposal;
- cancellation and partial failure leave an auditable state.

Use fakes for paid adapters and cloud providers. Do not run a real model or cloud
sandbox during implementation.

## Acceptance commands

```bash
uv run pytest
uv run ruff check .
uv run evallab proposal validate <fixture-proposal>
uv run evallab proposal plan <fixture-proposal>
```

Optionally execute only a bounded local Oracle/no-op fixture after confirming it
cannot reuse or overwrite an existing job directory.

## Handoff

Document the proposal schema, state machine, approval classes, idempotency key,
and recovery semantics. Demonstrate one blocked paid proposal and one successful
local control proposal. Do not claim the system is autonomous; the approval gate
is part of the intended architecture.
