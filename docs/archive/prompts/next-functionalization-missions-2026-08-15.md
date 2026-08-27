---
status: historical
audience:
  - builder
---

> **Archived work order**: Completed historical dispatch (M006-R/M009 merged). Deliverable record: docs/checkpoints/2026-08-16-m009-integration-flight.md. Board: agents/missions/ACTIVE.md.

# Next functionalization missions — 2026-08-15

These are temporary missions, not new permanent roles. The four stable lanes remain
Integration, Research, Tasks, and Platform. Give a mission to whichever strong coding
agent is available, record its exact agent/model in the handoff, and retire the mission
after merge.

## Observed starting state

- M006 produced PR #47 and CI is green, but the integrator's semantic review found four
  acceptance-level defects. M006-R below is required before merge.
- M007 is active and verifying. It has found that the repository has no human-registered
  tasks; `event-summary` is only a candidate and currently fails static admission.
- M006 deliberately ships fail-closed: its default worker has no live adapter and its
  `calibrated_judges_only` check returns false. The existing measured judge agreement is
  0.762987, below the 0.90 floor.
- Therefore the next honest milestone is an operational saved-response flight, not an
  always-on model-analysis daemon.

## Dispatch order

```text
NOW
├── M006-R repairs PR #47                    safe beside M007
└── M007 finishes, opens PR, then integrator reviews/merges it

AFTER BOTH MERGE
└── M009 integrator live flight (exclusive use of local services)

AFTER M009 PASSES — two build slots
├── M010 qualified stage-5 analysis runtime       Research + Platform
└── M011 first certifiable task pack              Tasks

WHEN EITHER SLOT OPENS
└── M012 unified operator cockpit                 Platform

AFTER M010 + M009
└── M013 restart-safe analysis service and soak   Platform + Research review

LATER, WHEN FEATURE BRANCHES ARE QUIET
└── M014 CI determinism and maintenance           Integration + Platform
```

Never exceed two active build PRs. M009 is an integrator acceptance exercise, not a
third build branch. M006-R and M007 are the two current slots. M010 and M011 can run in
parallel after integration. M012 should not overlap another
dashboard mission. M013 starts only after M010 proves the qualification and invocation
boundary. M014 waits until feature work is quiet because its test/CI lease is broad.

## Shared standing orders for M010–M014

```text
Lab: ~/Developer/eval-lab. Follow AGENTS.md and agents/{WORKFLOW,STRUCTURE,OWNERS,CHECKS}.md.
Create .worktrees/<mission> on role/<mission> from current origin/main; uv sync --locked.
Read the mission's referenced implementation and docs before editing. One writer per
worktree; stay inside the explicit lease. Record exact agent/model and update
agents/handoffs/<mission>.md at every stop. No API-key environment variables, paid model,
cloud sandbox, large benchmark, policy edit, task registration, publication, or approval.
Subscription calls still require the existing queue and Peter's authorization. Free local
Oracle/Nop controls only, concurrency <=2. Preserve raw jobs and promoted evidence bytes.
Run focused tests, full pytest, Ruff, scripts/premerge.sh, rebase origin/main, push, open the
named PR, and stop at review. The integrator owns conflicts and merge.
```

## M006-R — repair the guarded analysis worker

Assign now, preferably to the original M006 agent. It can run beside M007 because their
leases remain disjoint. Confirm the prior M006 session has stopped before reusing its tree.

```text
/goal Repair PR #47 until the shipped nightly path really stages completed trials after successful ingest, every frozen input is reverified, process crashes cannot strand leases, and completed sidecars reach the catalog without weakening the closed calibration gate.

Resume only the inactive ~/Developer/eval-lab/.worktrees/m006-analysis-worker worktree on
role/m006-analysis-worker; do not create a competing writer or new PR. Read AGENTS.md,
agents/{WORKFLOW,OWNERS,CHECKS}.md, the M006 prompt/handoff, and the integrator review on
PR #47. Preserve the existing M006 lease and record exact agent/model if the repair agent
differs. Do not edit policy/profiles/dashboard/tasks/raw evidence or add a live adapter.

Fix four blockers. (1) Wire default_worker(...).stage(default_job_roots(...)) into the real
nightly CLI composition only after completed_job_ingester succeeds. A staging failure must
produce a durable quarantine/error event, not be silently suppressed; prove the real CLI/
NightlyCycle composition, not only an injected unit seam. (2) Before admission, recompute
and compare result, trajectory, task, verifier, prompt, rubric, and profile identity. Missing
or changed evidence/prompt/rubric must fail closed with a precise reason before any adapter
call. If Harbor locks are the source of task/verifier digest truth, validate the current lock
bytes/fields needed to prove those frozen values. (3) Replace the ownerless permanent lease
marker with crash-recoverable ownership/age semantics. Prove concurrent live workers never
double-call, a dead owner before invocation is reclaimable, and a crash during/after a call
adopts durable output without a second call. Do not reclaim a demonstrably live lease.
(4) Give default_worker an idempotent facts.ingest_analysis_sidecar indexer using the normal
database URL; catalog failure leaves the sidecar adoptable and retryable.

Keep the default adapter absent and calibrated_judges_only false: the measured 0.762987
record does not qualify. Add focused regression tests for all four fixes, run the worker
cycle x3, full pytest, Ruff, scripts/premerge.sh, rebase current origin/main, push the same
branch, and update the existing PR/handoff with commands and evidence. Stop at review;
integrator re-reviews and merges. PR remains `M006: add guarded post-trial analysis worker`.
```

## M009 — integrator operational live flight

Do not hand this to a feature worker. The integrator runs it after M006 and M007 merge.

```text
/goal Prove the merged lab as one local, restartable Harbor-to-analysis product, record exact evidence, and turn every failure into a narrowly scoped follow-up instead of declaring success from fixture tests.

Use the current merged main checkout and the operations runbook. First preserve and
reconcile any dirty or stale primary checkout without deleting user work; if that requires
a judgment about Peter's files, stop and ask. Start only the repository PostgreSQL and
Phoenix services. Run doctor, then one real free event-summary Oracle and Nop control via
the guarded queue at concurrency 1. Confirm immutable Harbor completion, queue settlement,
catalog ingest, Parquet projection, experiment→job→trial joins, and Phoenix trace receipt.

Run the M006 worker through discovery/staging and a saved-response adapter only: pass,
reward-fail, and harness-exception evidence must become respectively completed/completed/
deferred, with one sidecar per eligible trial, valid citations, and zero model calls. Kill
the cycle once after sidecar write but before indexing, restart it, and prove adoption with
no duplicate sidecar/call. Rescan three times. Open the dashboard and explorer from the
same checkout; select the new job/trials and show reward, exception separation, artifacts,
trajectory availability, analysis lifecycle, provenance, and next action.

Write docs/checkpoints/<date>-operational-flight.md with commands, versions, job/trial/
analysis IDs, file paths, digests, screenshots only if useful, and observed versus inferred
claims. Do not patch product code opportunistically. If a defect blocks the flight, open
one minimal repair mission with reproduction and exact owning paths, then rerun M009 after
that repair merges. Stop services only if they were not already running. Acceptance is one
complete flight plus the crash/restart proof; no paid call and no raw-evidence mutation.
```

## M010 — qualified stage-5 analysis runtime

Model fit: strongest evidence/provenance engineer. Depends on M006 and M009.

```text
/goal Replace M006's hard-coded closed gate with a real tuple-specific qualification gate and a queue-authorized bounded analysis adapter, while remaining fail-closed until measured agreement reaches 0.90.

[Shared standing orders apply.] Read analysis_worker.py, facts.py, researchers.py,
calibrate.py, profiles.py, queue policy/schema, docs/{analysis-loop,analysis-worker,
agent-profiles}.md, and trajectory labels. Own src/evallab/analysis_runtime.py,
tests/test_analysis_runtime.py, tests/fixtures/analysis_runtime/,
research/calibration/trajectory-analysis/, docs/stage5-analysis-runtime.md, and
agents/handoffs/m010-analysis-runtime.md; minimal additive edits to analysis_worker.py,
schemas/database/CLI are allowed. Do not edit policy, profiles, dashboard, tasks, raw runs,
or existing calibration records.

Define a sealed, digest-addressed trajectory-analysis calibration corpus and append-only
QualificationRecord keyed by adapter, resolved model, adapter version, prompt, rubric,
output schema, and corpus digests. Score exact validity/category/earliest-step/citation
fields and report per-field plus mean agreement. Stub/saved outputs prove plumbing but can
never qualify. Existing postmortem calibration is not interchangeable with this rubric.

Implement a read-only bounded adapter by reusing the existing subscription researcher
invocation sandbox and ledger; no generic shell executor. Every live invocation must point
to a prior queue authorization record, pass M003 profile preflight, STOP, service, per-call,
daily-budget, evidence-digest, and exact qualification checks, and emit a durable attempt
record before invocation. Crash/retry must not double-call. Replace the hard-coded false
only with a lookup that still returns false when no exact measured record meets 0.90.

Add plan/status/stage-calibration commands; staging writes a reviewable queue item and
never invokes a model. Tests prove wrong/stale/partial/stub qualifications, unauthorized
calls, auth failure, ceilings, tampering, invalid citations, crash points, and a saved-
response full path. Do not claim the runtime is live-qualified unless Peter separately
approves a queued calibration and its measured record passes. PR `M010: qualify stage-5
analysis execution`; Research and Platform review; stop at review.
```

## M011 — first certifiable task pack

Model fit: careful Harbor task/verifier engineer. Depends on M007.

```text
/goal Use the merged M007 workbench to turn the existing event-summary candidate into a version-pinned, adversarially tested certification packet that Peter can knowingly register, without registering or publishing it.

[Shared standing orders apply.] Read task_workbench.py and its docs, task-registry.md,
event-summary task bytes and prior Oracle/Nop evidence, and the create-task guidance if
available. Own library/tasks/event-summary/, research/registration/candidates/event-summary*/,
tests/test_event_summary_task.py, docs/tasks/event-summary.md, and
agents/handoffs/m011-event-summary.md. Do not edit the workbench implementation, registry,
queue, policy, shared CLI, unrelated tasks, or promoted evidence.

Resolve the observed admission failures without weakening checks: pin agent and verifier
images/dependencies by immutable version or digest with provenance; add enough distinct
invalid solutions to exercise empty, malformed, fabricated, path/format, and extra-output
cases; preserve hidden test/golden isolation. Improve instructions only where a control or
adversarial case proves ambiguity. If changing a registered immutable version is detected,
create a new task version instead of editing it.

Run the deterministic workbench. Static checks must pass before controls. Then run exactly
three Oracle, one Nop, and all declared invalid solutions locally at concurrency 1. Require
Oracle 3/3 reward 1, Nop 0, every invalid 0, byte-identical verifier output across Oracle
runs, no leaked solution/golden bytes, and a byte-identical rebuilt packet. Classify any
Harbor/Docker failure as harness evidence, not task failure. Produce a concise human review
packet stating `admitted=false`, source/license/image/verifier/control digests, commands,
limitations, and the exact registration decision Peter must make. PR `M011: certify the
event-summary task candidate`; Tasks review; stop at review.
```

## M012 — unified operator cockpit

Model fit: product-minded Python/Streamlit engineer. Depends on M009; may run beside M010
or M011 only when its dashboard lease is exclusive.

```text
/goal Make one read-only Eval Lab UI show what ran recently, what is running, what is queued next, which tasks are certifiable, and how each completed trial moved into analysis, without making the operator launch a second Streamlit app.

[Shared standing orders apply.] Read status/explorer/analysis-worker/task-workbench models,
dashboard app+explorer, operator-demo, and the M009 checkpoint. Own dashboard/,
src/evallab/operator_cockpit.py, tests/test_operator_cockpit.py,
tests/fixtures/operator_cockpit/, docs/operator-cockpit.md, and
agents/handoffs/m012-operator-cockpit.md. No queue/approval/policy execution, task edits,
model calls, or raw-evidence writes; avoid shared CLI edits by keeping the existing
`evallab dashboard` entry.

Fold the explorer into the main app with stable task→experiment→job→trial→trajectory→
analysis navigation. Show Recent/Running/Next, queue state and reasons, task registry and
candidate certification, worker request/transitions, reward versus infrastructure failure,
cost/tokens/timing, artifacts, ATIF steps/tools/loops/verify-before-done, citation resolution,
qualification state, and exact provenance. Each value remains observed/derived/draft/
unavailable. Auto-refresh must retain selection and make a running trial or analysis visibly
advance; missing Postgres/Phoenix/Parquet/ATIF/sidecars stays navigable.

Next Action is copy-only and emits only real shell-safe commands. Jailed path resolution,
secret-shaped-field redaction, no hidden verifier rendering, duplicate-ID handling, and
malformed source isolation are mandatory. Fixture tests cover cold start through completed
analysis plus running/deferred/quarantined transitions. Run Streamlit AppTest and one M009
evidence click-through. PR `M012: unify the operator cockpit`; Platform review; stop at
review.
```

## M013 — restart-safe analysis service and soak

Model fit: operations/reliability engineer. Depends on M010 and the M009 flight.

```text
/goal Turn completion ingestion and analysis reconciliation into a restart-safe local service, then prove it under an accelerated soak without permitting autonomous spend or experiment approval.

[Shared standing orders apply.] Read automation/scheduler/queue/analysis runtime, operations,
execution tiers, M009 evidence, and LaunchAgent tests. Own
src/evallab/analysis_service.py, tests/test_analysis_service.py,
tests/fixtures/analysis_service/, scripts/launchd analysis templates,
docs/analysis-service.md, agents/handoffs/m013-analysis-service.md; minimal additive edits to
automation.py, CLI, and schedule installation are allowed. Do not edit policy, profiles,
tasks, dashboard, calibration records, or researcher semantics.

Implement one non-overlapping reconcile cycle: discover completed jobs, ingest/project,
stage analysis requests, recover durable states, and execute only requests carrying exact
queue authorization plus a passing M010 qualification. Unqualified/unauthorized work may
stage and defer forever with zero calls. Use a single-instance lock, bounded subprocesses,
atomic state, explicit heartbeats, stale-lock recovery, backoff, STOP, disk/service/auth
health, and graceful shutdown. Never approve or generate an experiment.

Add install/status/uninstall definitions consistent with existing LaunchAgents; install and
uninstall remain human-triggered. Tests inject clock/process/probes and cover duplicate
ticks, service overlap, crash at every phase, reboot recovery, midnight budget rollover,
missing services, malformed jobs, tampering, and log rotation. Run a four-hour-equivalent
accelerated soak using fixtures and saved responses, including repeated restarts, and prove
bounded files, stable digests, no duplicate calls/sidecars, and zero paid invocations. A real
24-hour main-checkout soak is a later integrator action after merge. PR `M013: add restart-
safe analysis service`; Platform + Research review; stop at review.
```

## M014 — CI determinism and maintenance

Model fit: senior build/test engineer. Run only when feature branches are quiet.

```text
/goal Make the repository's green signal reproducible and useful by removing host-state and wall-clock flakiness, exposing untested command surfaces, and proving any cleanup before deleting it.

[Shared standing orders apply.] Read CHECKS, workflows, pyproject, premerge, test fixtures,
and recent CI history. Own .github/workflows/, scripts/premerge.sh,
tests/test_ci_contract.py, docs/engineering.md, agents/handoffs/m014-ci-maintenance.md;
request a narrow lease before touching any feature test or pyproject/uv.lock. No feature
semantics, dependency upgrades, broad rewrites, generated files, or source deletion.

Reproduce and replace wall-clock-sensitive tests with injected clocks/barriers; make CI
cancel superseded heads, cache only safe immutable inputs, and ensure every shipped CLI
module imports and its `--help` surface executes on Python 3.12 and 3.14. Add a manifest-
based test that flags orphaned fixtures/docs/entry points without deleting them. Any proposed
deletion requires git/reference/import/runtime reachability evidence in the handoff and a
separate integrator decision.

Run the full local gate three consecutive times and record duration plus exact SHA; then
require exact-head GitHub checks. Do not weaken assertions, increase the type ratchet, mark
tests flaky, or add retries that hide failures. PR `M014: harden deterministic CI`; Integration
+ Platform review; stop at review.
```

## What Peter should assign next

Assign **M006-R now**, beside the already-running M007. Then let the integrator re-review
and merge #47 plus the M007 PR. Do not queue M010–M014 yet. Run M009 next. If M009 passes,
assign **M010 and M011 together**. When either slot
opens, assign M012. M013 is the first actual 24/7-enabling build, but only after M010 makes
the execution gate real. M014 is valuable maintenance, not the critical path.
