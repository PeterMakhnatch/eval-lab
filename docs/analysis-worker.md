# Guarded post-trial analysis worker

M006 (Research, Platform review). Code: `src/evallab/analysis_worker.py`.
Tests: `tests/test_analysis_worker.py` (46). Fixtures:
`tests/fixtures/analysis_worker/`. Composes existing machinery — discovery
(`results`), stage-5 execution and sidecars (`facts.run_trial_analysis`),
catalog indexing (`facts.ingest_analysis_sidecar`), profile preflight
(`profiles.preflight`, M003) — behind one idempotent state machine.

## The record

Every eligible completed trial gets exactly one `AnalysisRequest`:

- **Identity keys on the trial** (`sha256(job_id:trial_id)[:16]`), frozen at
  first sight. Rescans and restarts collide on the same record.
- **Content digests are frozen inside**: result, trajectory, task, verifier,
  rubric, prompt, and profile digests. Admission re-verifies them — changed
  evidence **quarantines** the record; it never silently mints a runnable new
  identity. A changed profile defers with `stale_identity`.
- **Transitions are append-only** (`transitions.jsonl`): pending → admitted →
  running → completed, with deferred(reason)/quarantined(reason) anywhere.
  State = last line; the store rebuilds from files alone.
- **Invocation attempts are append-only and fsynced** (`invocations.jsonl`).
  `invocation_started` is durable before the analyzer boundary. It is resolved
  only by a durable sidecar or an explicit operator disposition.

## Admission (ordered; each gate → zero calls + recorded reason)

1. `queue/STOP` → deferred
2. Evidence integrity (missing → quarantine, tampered → quarantine)
3. Profile identity unchanged (else deferred `stale_identity`)
4. Policy: `researcher-followups` rule exists, adapter listed, every
   `requires` entry passes an injected check. **`calibrated_judges_only`
   fails closed in the default composition** — the measured judge record is
   below the 0.90 floor, so real model calls stay deferred until calibration
   lands. That is the design working, not a bug.
5. Profile qualification (`verified_facts` non-empty) + M003 preflight
   (auth failure = operational deferral, never an agent failure)
6. Cost ceilings: per-call vs `per_job_cost_ceiling_usd`, projected daily vs
   `daily_cost_ceiling_usd`
7. Service health probe

Harness exceptions are staged as deferred
`harness_exception_not_agent_failure` — they are evidence about the harness
and are never sent to a model as agent behavior.

## Crash and concurrency safety (tested)

- Sidecar on disk without a `completed` transition → **adopted**, zero new
  calls. Crash before indexing → indexing retried idempotently on rescan.
- `invocation_started` without a sidecar is
  `ambiguous_invocation_requires_operator_resolution`. It never automatically
  replays a possibly paid invocation. An operator must either quarantine it or
  explicitly authorize retry; both actions are durable and attributed.
- A kernel `flock` serializes each request. Its metadata includes a unique
  owner token, PID, host, and acquisition time. Process death releases the
  lock; recovery overwrites stale metadata while holding the kernel lock.
  Release closes only the exact acquired file descriptor and never unlinks the
  lease path, so it cannot delete a replacement live owner's lease.
- Cycle×3 against fixtures: 2 calls total ever; cycles 2–3 are no-ops.

## Surfaces

```bash
uv run evallab analyze worker-plan      # read-only: what a cycle would do
uv run evallab analyze worker-status    # read-only: counts + per-request state
uv run evallab analyze worker-run-one <request-id>   # normal admission; never self-approves
uv run evallab analyze worker-resolve-ambiguous <request-id> \
  --action quarantine --actor <operator>
# `--action retry` is an explicit acknowledgement that the prior call may have charged.
```

`worker-run-one` in the default composition holds **no adapter** — reaching
execution without an operator-constructed adapter raises before any call.
Nightly (`automation.NightlyCycle`) accepts an optional `analysis_stager`
that may **discover and stage only**, inside the healthy branch; staging
freezes identity and cannot call a model by construction. Thrown staging
errors and normal returned quarantine/error counts each create one bounded,
durable queue event. Reasoning output
cannot approve anything: approval surfaces (`evallab approve`, policy edits)
are untouched by this mission.

## Sidecars

Written by the existing `facts.run_trial_analysis`: exact evidence citations
(validated against the trajectory; invalid citations produce
`validation_status: "invalid"` with errors, not a crash), usage/cost,
agent/adapter/model/version, output schema digest, and source digests.
Source evidence bytes are proven unmodified by a byte-level tree comparison
in tests, and `run_trial_analysis` additionally self-checks trial-tree
digests before returning.
