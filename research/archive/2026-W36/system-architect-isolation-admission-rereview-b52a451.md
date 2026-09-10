# System Architect — final isolation/admissibility re-review at `b52a451`

## Review target

- Read-only exact branch/worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final`, branch `fix/darwin-isolation-admission-final`.
- Exact new head: `b52a451641e3b10507e5907d8b129d1ba2f212a3`.
- Original replacement implementation: `20fecb1dab1a08038349ea1e260748212b68eb0f`.
- Review the complete replacement authority from base `ba95065b5ebe580b352ee370b08a2836a84f7c15..b52a451641e3b10507e5907d8b129d1ba2f212a3`, with focused attention on repair delta `20fecb1..b52a451`.
- Prior independent BLOCK report: `/tmp/system-architect-isolation-admission-final-review-20fecb1.md`.
- Parent closure brief: `research/inbox/eval-platform-isolation-parent-review-closure.md`.
- Do not edit, integrate, rebase, or run a model evaluation.

## Claimed closure to verify adversarially

1. Pending control finalization preserves the existing bound certification, approval identity/timestamp, and exact task runtime identity when no new certification packet is supplied. Changed certification, approver, approval time, package bytes, or runtime identity is refused. A registered pending record can never finalize with legacy/missing certification.
2. Trial admissibility has one canonical authority path. Alternate paths are refused; publication is exclusive/atomic and fsyncs the parent directory. Conflicting existing authority is refused.
3. The interpretation sidecar is causal only when the shared `TrialAnalysisSidecar` schema parses, status is valid, errors are empty, trial/source identity matches, the exact required file map matches, and every declared result/task/trajectory/citation digest matches live bytes. Invalid/malformed/drifted sidecars must fail closed.
4. `evaluated_at` for final trial admissibility is the immutable trial execution timestamp from `result.json`, not evidence observation time or verification time. Stale evidence cannot be made current by rerunning the verifier.
5. Registered nonbillable oracle/nop baseline controls carry explicit causal isolation authority in the frozen manifest, are re-projected for freshness and live-rebound at dispatch, and refuse missing binding, expired/stale evidence, runtime/probe drift, or direct queue bypass before producer execution.
6. The staged-control lifecycle test now drives campaign manifest creation, queue/Executor policy/dispatch, runner-producer injection, analysis ingestion, control-evidence discovery, and production registry finalization. It must demonstrate both oracle and nop routes and preserve the staged task identity end to end; handwritten artifact-only/direct-finalizer coverage is insufficient by itself.
7. All prior transport capture and Darwin isolation digests/authority remain unchanged; no calibration/model run is activated.

## Reported evidence to corroborate

- Focused admission/campaign/queue/registry/facts/outcome/benchmark/network matrix: 444 passed.
- Touched Ruff: passed.
- Ruff format check: passed.
- `git diff --check`: passed.
- Worktree reported clean.

## Deliverable

Write `/tmp/system-architect-isolation-admission-rereview-b52a451.md` with exact APPROVE/BLOCK verdict, evidence, and any remaining blocking findings tied to paths/symbols and a reproducible adversarial probe. Page the parent. Approval authorizes only later semantic replay onto the canonical integration spine; do not perform integration.
