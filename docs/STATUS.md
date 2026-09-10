---
status: living
audience:
  - operator
  - builder
  - runner
---

# Research status — 2026-09-10

Projection of live catalog, queue state, and `PROGRAM.json`.
Answers what happened yesterday and what is running now deterministically.

## RECENT (Yesterday: 2026-09-09)

- **factory-development/psf__requests-7502** — 1/3 `reward==1.0` via nop, oracle
- **factory-pr66-runtime-generation-001** — 1/1 `reward==1.0` via oracle
- **factory-requests7502-runtime-generation-001** — 1/1 `reward==1.0` via oracle
- **quality-review/requests7502-callable** — 1/1 `reward==1.0` via oracle
- **quality-review/requests7502-oracle** — 1/2 `reward==1.0` via nop, oracle
- **quality-review/requests7502-shortcut** — 0/1 `reward==1.0` via oracle

### Evidence Quality Ledger

- **Evaluated Trials:** 288 (Passed: 58, Warnings: 182, Failed: 36, Quarantined: 12)
- **Top Quarantine/Failure Reasons:** `missing_trajectory_file`: 36, `infrastructure_exception:Traceback (most recent call last):`: 12

## RUNNING NOW

Nothing in `queue/running/` or `queue/approved/`.

## NEXT

No queued work waiting in `queue/waiting/`, `queue/pending/`, or `queue/proposed/`.

### Program Ledger Next Actions

1. **EXP-S02-txn-recon-k** (`status: waiting`): Does changing only attempt count on transaction-reconciliation change interval width more than the point estimate?
   - *Blocker:* k=5 hits per_job_cost_ceiling and canary max_attempts=3. k=1 spec was approved in runner worktree and never scored on primary.
   - *Next Action:* Peter: register n=5 or raise ceiling / measure per-attempt cost from 2026-08-15 actual 0.079/3≈0.026 (would be <$3 at k=5) but canary still caps attempts at 3.
1. **EXP-S03-preamble-ab** (`status: designed`): Does a short contract-discipline preamble change Codex pass@3 on event-summary?
   - *Blocker:* Authorization, not code. The field gap this entry recorded is closed: ExperimentSpec.extra_instruction_path exists (src/evallab/schemas/__init__.py) and build_command emits --extra-instruction-path (src/evallab/execution_contracts.py); both verified working on main. What remains is that the treatment arm is billable codex and needs a named human authorisation before it can be dispatched.
   - *Next Action:* Peter authorises, then submit the treatment only; pair with the 2026-08-15 control. Do not submit a fake second control.
1. **EXP-S04-claude-vs-codex** (`status: designed`): Does claude-code complete a scored event-summary canary trial, and how does pass@3 sit beside Codex?
   - *Blocker:* Current availability of the Claude OAuth keychain item harbor-practice-claude-oauth is unresolved; the prior removed-worktree record reported it absent. Auth exceptions are harness, not capability.
   - *Next Action:* Peter decides whether a separate authorized workflow should verify/provision the keychain item; only then consider Study 04, without expanding to three tasks first.
1. **EXP-S05-curated-nominees** (`status: waiting`): What is Codex pass@5 on CURATOR's five nominated cards?
   - *Blocker:* Cards only (no task.toml here); not canary/*; k=5 exceeds canary max; estimated $4.17 exceeds $3. Representative was out_of_policy.
   - *Next Action:* Peter registers a slice or promotes nominees with digests. PROGRAM does not copy frontier-bench trees.
1. **EXP-S06-query-optimize-register** (`status: waiting`): Does standing policy admit Codex on lab-authored query-optimize, and is the family valid?
   - *Blocker:* out_of_policy for billable Codex. Poor canary (slow amd64 image, ~10 min/trial).
   - *Next Action:* Peter decides whether to register. Do not add to nightly canary suite.
1. **EXP-N2-event-summary-sol-vs-terra** (`status: designed`): On event-summary, does gpt-5.6-sol differ from the already-scored gpt-5.6-terra pin?
   - *Blocker:* Human decision: whether sol vs terra is still worth a night. Proposed spec uses registered/event-summary and a superseded hypothesis.
   - *Next Action:* Do not submit this draft. Do not approve/reject/delete the proposed spec from this role. Peter decides.
1. **EXP-N3-claude-code-event-summary** (`status: designed`): Can claude-code produce a scored event-summary canary trial?
   - *Blocker:* Claude keychain availability is unresolved; the prior removed-worktree record reported the item absent.
   - *Next Action:* Peter decides whether to verify/provision harbor-practice-claude-oauth in a separate authorized workflow; then reassess the existing Study 04 spec.

## TASK DECISIONS

Human-owned, unresolved decisions from active proposals and policy review.

- **EXP-S01-canary-codex-k3**: none on the 2026-08-15 scored jobs
- **EXP-S02-txn-recon-k**: k=5 hits per_job_cost_ceiling and canary max_attempts=3. k=1 spec was approved in runner worktree and never scored on primary.
- **EXP-S03-preamble-ab**: Authorization, not code. The field gap this entry recorded is closed: ExperimentSpec.extra_instruction_path exists (src/evallab/schemas/__init__.py) and build_command emits --extra-instruction-path (src/evallab/execution_contracts.py); both verified working on main. What remains is that the treatment arm is billable codex and needs a named human authorisation before it can be dispatched.
- **EXP-S04-claude-vs-codex**: Current availability of the Claude OAuth keychain item harbor-practice-claude-oauth is unresolved; the prior removed-worktree record reported it absent. Auth exceptions are harness, not capability.
- **EXP-S05-curated-nominees**: Cards only (no task.toml here); not canary/*; k=5 exceeds canary max; estimated $4.17 exceeds $3. Representative was out_of_policy.
- **EXP-S06-query-optimize-register**: out_of_policy for billable Codex. Poor canary (slow amd64 image, ~10 min/trial).
- **EXP-N1-html-js-official-tests**: tests/test_outputs.py is hidden in the separate verifier and must never be copied, mounted, or made runnable in the evaluated agent image.
- **EXP-N2-event-summary-sol-vs-terra**: Human decision: whether sol vs terra is still worth a night. Proposed spec uses registered/event-summary and a superseded hypothesis.
- **EXP-N3-claude-code-event-summary**: Claude keychain availability is unresolved; the prior removed-worktree record reported the item absent.

## SYSTEM HEALTH & OPERATIONAL SMOKE

- Catalog accessible: yes
- Operational smoke/control specs count: 0
- Active storm alarms: 0 (quiet: no alarms in window)
