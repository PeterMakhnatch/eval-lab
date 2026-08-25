# tau-Knowledge bounded cohort report

## Scope and pin

The eight-task cohort is immutable and source-selected from `banking_knowledge`:
`task_001`, `task_002`, `task_003`, `task_006`, `task_007`, `task_010`, `task_020`, and `task_021`.
The source checkout is tau2-bench tag `v1.0.1` at commit
`fc0055dc4e0a316c3f83133267fbd6faaa770992` (MIT). Source task and evaluation-criteria
SHA-256 values are recorded in [`cohort.manifest.json`](cohort.manifest.json).

The existing Harbor adapter is used without duplication: Harbor commit
`636a2d0295d3ee233666bcd7d77fa81f7f090a19`, adapter package `0.1.0`, at
`/Users/petermakhnatch/Developer/agent-evals/harbor/adapters/tau3-bench`. Generation
uses `TAU2_BENCH_ROOT` to point at the exact source checkout. The candidate-side
runtime overlay pins each generated runtime Dockerfile to the same tag and verifies
`git rev-parse HEAD`; before/after template hashes and generated task hashes are in
[`runtime-pin-evidence.json`](runtime-pin-evidence.json).

## Semantic projection

The source evaluation criteria project to credit-card action, referral workflow,
and discoverable-user-tool workflow rows in [`semantic-projection.json`](semantic-projection.json).
These are source facts only. They are not observed agent behavior, retrieval results,
gold recall, or rewards. Retrieval rows are intentionally absent because no query/result
source IDs were exposed by a trial.

## Controls and gate

[`config/run.yaml`](config/run.yaml) and
[`scripts/tau_knowledge/run_controls.py`](../../../scripts/tau_knowledge/run_controls.py)
sequence one reference control, one oracle control, a clean-reset oracle repetition,
and at most one Luna attempt per task. The Luna phase requires all three per-task
control status values to be `passed`; missing status fails closed. No Luna attempt was
made.

The control attempts are recorded in [`evidence/control-attempts.json`](evidence/control-attempts.json).
Oracle startup was blocked by the absent `OPENAI_API_KEY` required by the tau2 user
simulator; the reference invocation was initially rejected because Harbor does not
register the adapter-local name, and the config now uses its external import path.
No ATIF, verifier reward, or agent reward was produced. Infra and agent status remain
separate, and unknown evidence is not treated as failure or zero.
