Status: review-wanted
Last: Second repair rebased on 903abe4; 46 focused + 418 full tests, Ruff, and premerge green
Next: Integrator re-review of PR #47 — stop at review, never self-merge
Blockers: none

# M006 handoff — guarded post-trial analysis worker

**Executing agent/model (recorded per mission order): Claude Code
(interactive session), model claude-opus-5[1m] (Opus 5, 1M context).**

Lease: src/evallab/analysis_worker.py, tests/test_analysis_worker.py,
tests/fixtures/analysis_worker/, docs/analysis-worker.md, this file; minimal
additive wiring in cli.py (three analyze subcommands + dispatch) and
automation.py (optional analysis_stager param, invoked stage-only in the
healthy branch). Untouched: dashboard/explorer, policy ceilings, profile
definitions, tasks/verifiers, registry, source evidence bytes, DISCOVERIES,
experiment agenda, ACTIVE.md, queue.py, schemas.py, database.py.
(schemas/database wiring turned out unnecessary — the request store is
file-based and rebuildable; indexing reuses facts.ingest_analysis_sidecar
via the injected indexer.)

## Design decision worth reviewing

Request identity keys on the TRIAL (job_id:trial_id), not on content. First
freeze wins; content digests are frozen inside and re-verified at admission.
My first draft keyed identity on content — tests caught that tampered
evidence then minted a fresh runnable identity instead of quarantining.
The trial-keyed design makes "one analysis record per trial" structural.

## Evidence

```
$ uv run pytest tests/test_analysis_worker.py -q
.......................                                                  [100%]
23 passed
$ uv run pytest -q          # full suite
108 passed
$ bash scripts/premerge.sh
premerge green: Python 3.12; ty 28 <= 28
```

Deterministic worker cycle x3 (mission requirement, saved-response adapter):

```
cycle 1: discovered=3 staged=3 calls=2 completed=2 deferred=1
cycle 2: discovered=3 staged=0 calls=0 completed=0 deferred=0
cycle 3: discovered=3 staged=0 calls=0 completed=0 deferred=0
total adapter calls: 2
store state: {"completed": 2, "deferred": 1}
```

(The deferred=1 is the harness-exception trial:
harness_exception_not_agent_failure. ty briefly rose to 29 from a pydantic
**kwargs construction; fixed with model_validate, baseline untouched.)

Coverage mapping to the mission list: completion hook (stage-after-ingest =
stage() over job roots; idempotent), pass/fail/harness exception, idempotent
rescan (x3), crash before call (re-admission path), crash after call
(sidecar adoption, zero re-calls), crash before index (flaky indexer retry),
concurrent workers (lease), stale evidence (profile stale_identity),
tampered evidence (post-freeze mutation -> quarantine), auth failure
(preflight defer, not agent failure), STOP, per-call + daily ceilings,
invalid citations (invalid sidecar, no crash), retry/quarantine reasons,
rebuild (fresh RequestStore from files), M005-compatible status shape,
zero writes to source evidence (byte-level tree equality).

## For the reviewers

- Research: the calibrated_judges_only gate fails closed in default_worker —
  live analysis stays deferred until a measured calibration record meets the
  0.90 floor. Confirm this matches the research agenda's intent.
- Platform: cli.py dispatch added inside the existing analyze branch;
  automation.py adds one optional constructor param + one guarded stage-only
  call. Both diffs are minimal and additive by inspection.

## Repair round (2026-08-15, integrator review on PR #47)

Same executing agent/model as the original: Claude Code (interactive),
claude-opus-5[1m]. Same lease + minimal additive cli.py/automation.py wiring.

**Blocker 1 — nightly wiring.** `automation.py`: staging now runs strictly
AFTER `completed_job_ingester` succeeds (inside its `else`), and a staging
failure appends a durable `analysis_stage_failed` queue event (never
suppressed). `cli.py`: the real nightly composition passes
`analysis_stager=_nightly_analysis_stager(root)`, which is
`default_worker(root).stage(default_job_roots(root))`. Tests prove ordering
(`ingest` then `stage`), skip-on-failed-ingest, the durable event, and the
exact CLI callable staging real requests from fixture evidence.

**Blocker 2 — full frozen-input reverification.** `admit()` now recomputes,
before any adapter call: result, trajectory, lock bytes (`lock_sha256`, new
frozen field), task_digest + verifier_digest re-derived from CURRENT lock
bytes via the same `facts._task_digest`/`_verifier_digest` used at freeze,
prompt, rubric, and profile digest. Missing evidence/prompt/rubric →
quarantine `evidence_missing:<what>`; changed evidence/lock/task/verifier →
quarantine `evidence_tampered:<what>`; changed prompt/rubric/profile →
defer `stale_identity:<what>`. Also fixed: freeze previously used getattr
fallbacks that froze task/verifier digests as None — they now come from the
lock truth and are asserted non-null in tests.

**Blocker 3 — crash-recoverable leases.** Lease file now records
{pid, acquired_at, host}. A demonstrably live owner is NEVER reclaimed
(age is irrelevant when liveness is provable — tested with an aged lease
owned by the live test process). Dead-pid and corrupt leases are reclaimed
atomically (rename-then-recreate O_EXCL). Tests: dead-owner reclaim runs
exactly once; live-owner defers with zero calls; corrupt lease reclaims;
crash-during-call (dead lease + durable sidecar, completed transition lost)
adopts with ZERO additional calls.

**Blocker 4 — default indexing.** `default_worker` wires
`_default_indexer(root)`: `database.initialize(url)` +
`facts.ingest_analysis_sidecar(url, path, root)` on the normal
`database_url_from_environment()`. Catalog failure raises before the
completed transition, leaving the sidecar adoptable; recovery test completes
it without a re-call.

Unchanged on purpose: default adapter absent (`_no_adapter` raises), and
`calibrated_judges_only` stays False — the measured 0.762987 record does not
meet the 0.90 floor.

## Repair evidence

```
$ uv run pytest tests/test_analysis_worker.py -q
37 passed
$ uv run pytest -q
122 passed
$ uv run ruff check .
All checks passed!
$ bash scripts/premerge.sh
premerge green: Python 3.12; ty 28 <= 28

cycle 1: staged=3 calls=2 completed=2
cycle 2: staged=0 calls=0 completed=0
cycle 3: staged=0 calls=0 completed=0
total adapter calls: 2
```

Rebased onto current origin/main before the repair; pushed to the same
branch; no new PR opened.

## Second repair round (2026-08-15, integrator re-review)

**Executing agent/model:** OpenAI Codex interactive agent, GPT-5. The runtime
did not expose a more specific model variant. The previous Claude session was
confirmed finished before this agent became the sole writer in the worktree.

The new adversarial tests were written first against `b494e50`. The focused
run produced **37 passed, 6 failed**, reproducing every remaining defect:
call-returned/no-sidecar replayed the adapter; ambiguity had no operator
resolution; lease APIs had no owner tokens and release was unconditional; and
a normal stage quarantine produced no durable event.

**Paid-call ambiguity.** Each invocation now has an append-only,
fsynced `invocations.jsonl`. `invocation_started` lands before entering
`run_trial_analysis`. The sidecar is fsynced and atomically published before
that attempt is resolved. A start with neither resolution nor sidecar defers
as `ambiguous_invocation_requires_operator_resolution` and never calls again.
A sidecar is adopted safely. Otherwise the operator must use
`worker-resolve-ambiguous --action retry|quarantine --actor ...`; the action
and actor are durable. Resolution itself takes the request lease and refuses
to race a still-live invocation.

**Lease ownership.** The rename/unlink stale-reclaim protocol was replaced
with `flock(LOCK_EX|LOCK_NB)` on a stable lease file plus a unique owner token.
Process death releases the kernel lock; stale metadata is overwritten only
while the new owner holds it. Release unlocks/closes only its exact file
descriptor and never deletes or rewrites the lease path. Tests cover a live
owner, two stale reclaimers, and deterministic path replacement between
ownership and release; the replacement owner's token and lock survive.

**Nightly returned failures.** After the real post-ingest stager returns,
`NightlyCycle` totals its `quarantined` and `errors` mappings and writes one
`analysis_stage_reported_issues` queue event. The reason is capped at 512
characters. The real CLI stager is exercised with a missing frozen prompt;
its returned `evidence_unreadable=1` is present in the durable event.

Unchanged: post-ingest ordering, all frozen digest checks, catalog retry,
default `_no_adapter`, and `calibrated_judges_only=False` at measured
0.762987.

## Second repair evidence

```text
# Before implementation, with adversarial tests only
.venv/bin/pytest tests/test_analysis_worker.py -q
37 passed, 6 failed

# Repaired/rebased implementation
.venv/bin/pytest tests/test_analysis_worker.py -q
46 passed

.venv/bin/ruff check .
All checks passed!

bash scripts/premerge.sh
418 passed in 17.33s
SMOKE PASS both-stores-agree
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 28
```

Rebased without conflicts onto `origin/main` at `903abe4`. No model, Docker,
cloud, paid call, task, verifier, policy, profile, dashboard, or raw evidence
was invoked or modified. Existing PR #47 remains the only PR; stop at review.
