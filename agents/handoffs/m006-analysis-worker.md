Status: review-wanted
Last: Worker + 23 tests + cycle-x3 proof + premerge green; PR next
Next: PR "M006: add guarded post-trial analysis worker" — independent Research + Platform review; stop at review
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
