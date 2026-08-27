---
status: historical
audience:
  - builder
---

> **Archived work order**: Completed historical dispatch (M005/M006/M007 merged). Living contracts: docs/run-explorer.md, docs/analysis-worker.md, docs/task-workbench.md. Board: agents/missions/ACTIVE.md.

# Functionalization missions — 2026-08-15

The lab already has Harbor execution, a guarded queue, raw evidence, catalog +
Parquet facts, comparison/reporting, Phoenix shipping, a dashboard, scheduled
ticks/nightly work, and structured analysis models. This phase turns those
pieces into an operator-visible product and a reliable completion-to-analysis
loop. It does not add another orchestrator, database, or autonomous approver.

## Dispatch order

```text
MERGED
├── M002 operational slice                 PR #42
├── M003 subscription profiles             PR #43
└── M005 run + analysis explorer           PR #44

NOW
├── M006 post-trial analysis worker        released; already assigned
└── M007 task-quality workbench            safe to dispatch in parallel

AFTER M005 + M006 MERGE
└── integrator live flight: services, real free control, analysis, UI, recovery
```

Never exceed two active build PRs. A worker stops at review; the integrator
rebases, checks, merges, updates the board, and cleans the worktree.

## M005 — interactive run and analysis explorer

Model fit: product-minded senior SWE with Streamlit/data-interface judgment.
Start only after M002 merges.

```text
/goal Build a read-only run and analysis explorer until Peter can select a task, job, trial, trajectory, or analysis and understand what ran, what happened, why it was classified, and the exact safe command for the next action.

Setup after M002 merges: cd ~/Developer/eval-lab && git fetch origin && git worktree add .worktrees/m005-explorer -b role/m005-explorer origin/main && cd .worktrees/m005-explorer && uv sync --locked. Read AGENTS.md, agents/{WORKFLOW,CHECKS,OWNERS}.md, agents/missions/ACTIVE.md, docs/{architecture,analysis-loop,operator-demo}.md, dashboard/, status/results/atif/facts/report modules, and fixtures. M005 is Platform. Record exact agent/model. Own only src/evallab/explorer.py, tests/test_explorer.py, tests/fixtures/explorer/, dashboard/explorer.py, dashboard/app.py, dashboard/README.md, docs/run-explorer.md, agents/handoffs/m005-explorer.md. Do not edit CLI, queue, execution, policy, profiles, analysis generation, raw runs/evidence, tasks, registry, or ACTIVE.md. No live model, cloud, benchmark, or state-changing dashboard control.

Reuse raw Harbor jobs, PostgreSQL metadata, Parquet, and immutable analysis sidecars. Add linked views: Tasks (registration/control state); Jobs/Trials (agent/model/config/reward/exception/timing/cost); Trajectory (ordered steps, tool calls, exits, repetitions, verify-before-done); Artifacts; Analysis (status, validity/category, summary, evidence citations resolved to source step/tool, alternatives, provenance); and Next Action. Every field states observed/derived/draft/unavailable. Infrastructure exceptions are visually separate from reward failures. Never rank incomparable cohorts.

Next Action emits copyable `evallab` commands for local oracle/nop, analysis planning, queue submission/approval, and Harbor single-trial inspection, but executes nothing. Cold start and malformed/missing Postgres, Parquet, ATIF, sidecars, or files remain navigable. Prevent path escape and never render secrets or hidden verifier content.

Fixture tests cover pass/fail/harness exception, missing trajectory, tool loop, verification behavior, artifact links, valid/invalid analysis citations, duplicate IDs, path escape, cold start, status/explorer consistency, and zero writes. Render/smoke the Streamlit view against committed evidence and save verification details in the handoff; do not commit screenshots unless the repository requires them. Run pytest, Ruff, premerge, exact-head CI. PR `M005: add run and analysis explorer`; stop at review.
```

## M006 — idempotent post-trial analysis worker

Model fit: strongest cross-system SWE with evidence/provenance discipline.
Start only after M002 and M003 merge. It may run beside M005 because it cannot
touch dashboard/explorer files.

```text
/goal Build an idempotent completion-to-analysis worker until every eligible completed trial has one provenance-frozen pending, completed, deferred, or quarantined analysis record and no model can run without profile and policy admission.

Setup after M002+M003 merge: cd ~/Developer/eval-lab && git fetch origin && git worktree add .worktrees/m006-analysis-worker -b role/m006-analysis-worker origin/main && cd .worktrees/m006-analysis-worker && uv sync --locked. Read AGENTS.md, governance/checks, docs/{analysis-loop,operations,execution-tiers,operator-demo}.md, queue/automation/researchers/analysis/facts/profile code and tests. M006 is Research with Platform review. Record exact agent/model. Own only src/evallab/analysis_worker.py, tests/test_analysis_worker.py, tests/fixtures/analysis_worker/, docs/analysis-worker.md, agents/handoffs/m006-analysis-worker.md, and minimal additive schemas/database/automation/queue/CLI wiring. Do not edit dashboard/explorer, policy ceilings, profile definitions, tasks/verifiers, registry, source evidence bytes, DISCOVERIES, experiment agenda, or ACTIVE.md. No live model/cloud/benchmark call.

On successful ingest, discover eligible completed trials and freeze AnalysisRequest identity from experiment/job/trial/result/trajectory/task/verifier/rubric/prompt/profile digests. Persist reconstructible requests and append-only transitions: pending, admitted, running, completed, deferred(reason), quarantined(reason). Duplicate scans/restarts create no duplicate calls or sidecars. Harness/auth/infrastructure failures are not agent failures. Missing/tampered evidence quarantines before a call. Completed immutable sidecars retain exact evidence citations, usage/cost, model/adapter/version, output schema, and source digests, then index through the existing rebuildable catalog.

Execution uses the existing researcher-followups policy and M003 profile preflight. STOP, absent qualification, cost/call ceiling, stale identity, duplicate, or unhealthy services produces zero calls. A saved-response adapter proves the whole path in tests; no generic shell executor. Add read-only plan/status/run-one CLI surfaces, but run-one still requires normal admission and never approves itself. Nightly may discover/stage only; reasoning output cannot approve a new experiment.

Tests cover completion hook, pass/fail/harness exception, idempotent rescan, crash before/after call/sidecar/index, concurrent workers, stale/tampered evidence, auth failure, STOP, ceilings, invalid citations, retry/quarantine, rebuild, and M005-compatible status shape. Run deterministic worker cycle x3, pytest, Ruff, premerge, exact-head CI. PR `M006: add guarded post-trial analysis worker`; independent Research + Platform review; stop at review.
```

## M007 — task-quality workbench

Model fit: strong task/verifier engineer. Start when M005 or M006 opens a
slot. It is a Tasks-lane project and does not touch the dashboard or worker.

```text
/goal Build a deterministic Harbor task-quality workbench until an author can inspect a candidate, run free controls, test verifier discrimination and isolation, and produce a review packet without self-registering or publishing the task.

Setup when a build slot opens: cd ~/Developer/eval-lab && git fetch origin && git worktree add .worktrees/m007-task-workbench -b role/m007-task-workbench origin/main && cd .worktrees/m007-task-workbench && uv sync --locked. Read AGENTS.md, governance/checks, docs/task-registry.md, create-task guidance if available, existing registry/fetch/task fixtures, and several registered tasks. M007 is Tasks. Record exact agent/model. Own only src/evallab/task_workbench.py, tests/test_task_workbench.py, tests/fixtures/task_workbench/, library/synthetic/, research/registration/candidates/, docs/task-workbench.md, agents/handoffs/m007-task-workbench.md. No shared CLI/queue/policy/profile/dashboard/analysis/ACTIVE edits in v1. No model/cloud/paid run; free oracle/nop at concurrency <=2 only after unit acceptance.

Implement inspect -> static checks -> isolated oracle/nop controls -> verifier mutation/adversarial checks -> deterministic certification packet. Validate task.toml/instructions/environment/solution/tests/artifacts/timeouts/metadata, hidden-test and solution isolation, network policy, pinned provenance/license, deterministic verifier output, oracle success, nop rejection, several invalid-output rejections, and no leaked golden data. Distinguish task defect, harness defect, and agent failure. Preserve failed candidates and exact diagnostics.

Candidate and certification records contain all source/config/image/verifier/control digests and commands. Rebuild is byte-identical. A candidate cannot queue, register, freeze, publish, or edit policy; only a separate human-created registry record can admit it. Provide `python -m evallab.task_workbench plan|check|packet` so v1 needs no shared CLI wiring.

Fixture tests cover valid task plus missing files, path escape, hidden leak, nondeterminism, permissive verifier, false-negative verifier, network use, unpinned dependency, forged registration, and interrupted controls. Exercise the workbench on one existing registered task and one intentionally bad fixture; no task bytes changed. Run pytest, Ruff, premerge, exact-head CI. PR `M007: add task-quality workbench`; Tasks reviewer required; stop at review.
```

## What “24/7” means after this wave

The always-on system may continuously health-check, reconcile the approved
queue, ingest completed Harbor jobs, project deterministic facts, stage
eligible analysis requests, render status/digests, and defer safely. It must
not continuously invent and approve paid experiments. After M005 and M006,
the integrator runs a live flight and recovery test; only then should a later
mission wire the analysis worker into launchd and conduct a 24-hour soak.
