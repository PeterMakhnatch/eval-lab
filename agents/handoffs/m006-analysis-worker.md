Status: review-wanted
Last: Fourth repair of 95d31e4 — the one remaining blocking defect fixed (the sidecar/ dirent is now created with _durable_mkdir, so the name proving a paid result exists is fsynced into the request directory); 53 focused tests and Ruff green; docs/analysis-worker.md refreshed
Next: Integrator re-review of PR #47 at the pushed head of role/m006-analysis-worker (code at d454bbe, handoff commit on top); the five green checks at 95d31e4 are invalidated by this repair and must run again — never self-merge
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


## Third repair round (2026-08-15, independent exact-head review of 1f4cf6f)

Verdict was `incorrect` with four blocking defects and one optional
hardening. All five are fixed. Each defect was reproduced against `1f4cf6f`
before the fix — the six new tests were written first and observed failing.

**1. Journal dirent was not durable — `RequestStore._append_invocation_event`
and `RequestStore.freeze`.** `_append_invocation_event` fsynced the journal
bytes but never the directory that names them, so `O_CREAT` on
`invocations.jsonl` left the dirent in the parent's dirty cache. A
host-level crash could erase the journal, `unresolved_invocation` would
return None, and the guarded second billable call would be issued. New
`_fsync_directory` and `_durable_mkdir` helpers fsync the dirent of every
level a write creates; `_durable_replace` now shares them. `freeze` had no
fsync at all and now makes `request.json`'s bytes, its dirent, and the new
request directory durable — the request directory matters as much as the
journal, because losing it lets a rescan re-mint the same deterministic
identity with no journal to stop a second call.

**2. Ambiguity journal armed before a locally provable failure —
`AnalysisWorker.run_one`.** The `_no_adapter` sentinel is now detected before
`begin_invocation` and defers with reason `adapter_not_wired`; no journal
entry, so no operator ceremony. Placed *after* admission on purpose: an
evidence or policy verdict is more informative than a configuration one and
keeps `policy_requirement_unmet:calibrated_judges_only` observable as the
CLI's real answer today (see the review note below).

**3. Permanent eligibility deferrals were enforced only in `run_cycle` —
`AnalysisWorker.run_one` (new module predicate `_is_permanent_deferral`).**
The rule now lives in one predicate used by `run_one`'s state guard and by
the `run_cycle` loop; `run_cycle` behaviour is unchanged (it still skips
without calling `run_one`, so `CycleReport` counts are identical).

**4. The fail-closed default was unpinned — `tests/test_analysis_worker.py::
test_default_worker_stays_fail_closed_for_live_analysis`.** Asserts
`default_worker`'s adapter is `_no_adapter`, that the sentinel raises,
that `calibrated_judges_only()` is False, and — end to end — that a fully
eligible trial defers on `policy_requirement_unmet:calibrated_judges_only`
with zero calls and no armed journal. It also asserts an explicitly supplied
adapter is still honoured, so the test cannot be satisfied by breaking
injection. The gate and the default are unchanged; only the pin is new.

**5. Optional, taken — `AnalysisWorker._release_lease` and
`RequestStore.record_lease_replacement`.** Both `finally` blocks now go
through `_release_lease`, which on a false ownership result appends
`lease_replaced_during_execution` to the invocation journal. Deliberately
*not* `transitions.jsonl`: state is the last transition line, so an audit
note there would overwrite the reason the state machine reads back — it
would have silently erased the permanence signal fix 3 depends on.

### One review claim needs a correction

The review's stated consequence for defect 2 ("the request is stuck needing
the operator ceremony") is **not reachable through the CLI at `1f4cf6f`**.
`policy/standing-approvals.yaml` requires `calibrated_judges_only` on the
`researcher-followups` rule, and `admit` evaluates policy requirements before
`run_one` reaches `begin_invocation`, so today every CLI `worker-run-one`
defers at the closed calibration gate. The defect is real but latent: it
fires the moment the gate opens — which is this PR's stated future — or for
any caller that composes `default_worker(root)` against a policy without that
requirement. Proven both ways below. The fix stands; the severity is
"latent trap", not "currently misbehaving".

### Third repair evidence

```text
# reviewed head 1f4cf6f, loaded from git and driven with the gate open
# (defect 2) and with a counting adapter (defect 3):
PRE-FIX run_one raised: no analysis adapter is wired; ...
PRE-FIX journal events: ['invocation_started']
PRE-FIX unresolved attempt: True
PRE-FIX next run_one verdict: ambiguous_invocation_requires_operator_resolution
PRE-FIX harness-exception run_one: completed None adapter calls: 1

# the six new tests against 1f4cf6f source, before the repair
uv run pytest tests/test_analysis_worker.py -k "durable or unwired or permanent or fail_closed or lease_replacement"
5 failed, 1 passed        # fail-closed defaults were already correct, just unpinned

# after the repair
uv run pytest tests/test_analysis_worker.py -q
52 passed
uv run ruff check src/evallab/analysis_worker.py tests/test_analysis_worker.py
All checks passed!
uvx ty@0.0.71 check src/ --output-format=concise
Found 28 diagnostics        # ratchet 33
uv run pytest tests/test_repository_contract.py tests/test_ci_coverage.py \
    tests/test_program_contract.py tests/test_pipeline.py -q
30 passed
```

Live proof through the production entrypoint (`evallab analyze worker-run-one`
with fixture jobs staged into this worktree's gitignored `runs/`, scratch
state since removed):

```text
# harness-exception request — defect 3 path, now permanent in run_one
{"state": "deferred", "reason": "harness_exception_not_agent_failure"}
# eligible request — the closed calibration gate, zero journals armed
{"state": "deferred", "reason": "policy_requirement_unmet:calibrated_judges_only"}
find derived/analyses/worker/requests -name invocations.jsonl | wc -l  ->  0

# same default_worker object with the gate flipped in-process only
# (nothing persisted, no provider reachable): defect 2 path
transition: deferred adapter_not_wired
invocation events: []   unresolved: None   sidecar: False
```

Capability labels: defects 1–5 **proven live** for the run_one/CLI paths shown
above and **fixture-proven only** for the crash-durability property (fsync
syscalls are asserted by inode; no host crash was staged). Full suite,
`scripts/premerge.sh`, and CI are the Integrator's to run — **pending in PR**.

Scope: only `src/evallab/analysis_worker.py`,
`tests/test_analysis_worker.py`, and this file changed. No rebase (branch left
on `1f4cf6f`'s history), no merge, no PR opened — #47 stays the only PR. No
model, paid call, Docker, cloud, policy, profile, or evidence byte touched;
the calibration gate is still closed and the default adapter is still
`_no_adapter`.

## Fourth repair round (2026-08-15, independent exact-head re-review of 95d31e4)

**Executing agent/model:** Claude Code subagent, claude-opus-5. Lease this
round: `src/evallab/analysis_worker.py`, `tests/test_analysis_worker.py`,
`docs/analysis-worker.md`, this file. Nothing else touched.

The re-review accepted four of the five third-round repairs and found exactly
one remaining P1: **the durability hole one level above the one already
fixed.**

**The defect — `AnalysisWorker.run_one`.** The per-request sidecar directory
was created with `sidecar_path.parent.mkdir(parents=True, exist_ok=True)`, so
the `sidecar/` dirent inside the request directory was never fsynced.
`_durable_replace` fsyncs the sidecar bytes and `sidecar/` itself, which
persists `analysis.json` *within* `sidecar/` — but per `_durable_mkdir`'s own
docstring, fsyncing a directory does not persist that directory's entry in its
parent. The only request-directory fsync on this path is the `O_CREAT` branch
of `_append_invocation_event`, i.e. during `begin_invocation`, strictly before
`sidecar/` exists.

Reachable failure: a host crash after the durably fsynced
`resolve_invocation` but before writeback of the request directory. On
recovery the journal shows the attempt resolved, so `unresolved_invocation`
returns None and the ambiguity guard stays quiet; and `sidecar_path.is_file()`
is False because the `sidecar/` dirent was lost, so the adoption guard stays
quiet too. `run_one` re-admits and issues a **second billable provider call**,
losing analysis already paid for. The durable ordering was inverted: the
record asserting the attempt was resolved was fsynced, the name proving a
result exists was not — and the module docstring already claimed the sidecar
was covered, so the stated invariant was false.

**The fix, one line.** `run_one` now calls the existing
`_durable_mkdir(sidecar_path.parent)`, which fsyncs the request directory for
each level it creates. Nothing else in the module changed.

### Audit of every mkdir reachable from run_one and resolve_ambiguous

Requested by the reviewer before committing; walked exhaustively.

- `analysis_worker.py` has exactly three `mkdir` call sites (`grep '\.mkdir\('`
  plus `makedirs`): inside `_durable_mkdir` itself, `RequestStore.freeze`
  (already `_durable_mkdir`), `RequestStore.acquire_lease`, and the `run_one`
  site now repaired.
- `RequestStore.acquire_lease` — `self._lease_path(request_id).parent.mkdir(...)`
  is the *request* directory, plain. **Not a hole, left alone.** On the
  `run_one` path it is unreachable as a creator: `run_one` opens with
  `self.store.load(request_id)`, which reads `request_dir/request.json` and
  raises before the lease if the directory is absent, so `freeze`'s durable
  `_durable_mkdir` always got there first. `resolve_ambiguous` does not load
  first, so it *could* create the directory — but only to then find no journal
  and raise `request has no ambiguous invocation to resolve`, leaving an empty
  orphan. Losing that dirent loses no proof and cannot cause a second call;
  the lease file itself is advisory `flock` state that no recovery decision
  reads, and it is re-created on the next acquisition.
- `RequestStore.append` (`transitions.jsonl`) creates no directory.
- `facts.run_trial_analysis`, reachable from `run_one`, does
  `(destination_root / analysis_id).mkdir(parents=True, exist_ok=False)`. That
  is a *staging* name under `sidecar/`; `run_one` then `_durable_replace`s the
  file to the stable `sidecar/analysis.json` and fsyncs `sidecar/`. Recovery
  reads only the stable path, so the staging dirent is not durability-critical
  (and `facts.py` is outside this lease).
- `facts.ingest_analysis_sidecar` (the default indexer, called from
  `_complete`) writes to Postgres and creates no directory.
- `admit`, `preflight`, `results.load_jobs`, `unresolved_invocation`,
  `invocation_events`, `resolve_invocation`, `_release_lease`,
  `record_lease_replacement`: no directory creation.

**Conclusion: one instance, now fixed. No second instance found.**

### Doc refresh (`docs/analysis-worker.md`)

Three stale claims corrected, all describing behaviour the third round
changed: the header test count (46 → 53); the ordered admission list gained
the `adapter_not_wired` gate as item 8, enforced in `run_one` after every
admission gate and before `begin_invocation`; and the surfaces section no
longer says the default composition "raises before any call" — the
`_no_adapter` sentinel is detected, never invoked, and the run defers with
reason `adapter_not_wired` without arming the journal.

### Fourth repair evidence

The new test was written first and observed failing at `95d31e4`, on the
ordering assertion specifically (its mkdir precondition passed):

```text
# at 95d31e4, new test only, before the one-line fix
uv run pytest tests/test_analysis_worker.py::test_sidecar_directory_dirent_is_durable -q
FAILED - AssertionError: the sidecar/ dirent is never fsynced into the request directory

# after the fix
uv run pytest tests/test_analysis_worker.py::test_sidecar_directory_dirent_is_durable -q
1 passed

uv run pytest tests/test_analysis_worker.py
53 passed in 1.03s

uv run ruff check src/evallab/analysis_worker.py tests/test_analysis_worker.py
All checks passed!
```

`_record_durability_events` extends the third round's inode harness: it records
`mkdir` and `fsync` as an ordered event log keyed by `(device, inode)`, because
dirent durability is an ordering property, not set membership. The test drives
a full `run_one` and asserts a request-directory fsync appears *after* the
sidecar directory's creation event.

Capability label for this repair: **fixture-proven only** — it asserts fsync
syscalls by inode and does not stage a real host crash.

Scope: `src/evallab/analysis_worker.py`, `tests/test_analysis_worker.py`,
`docs/analysis-worker.md`, and this file. `git diff --name-only 95d31e4..HEAD`
lists exactly those first three; `cli.py` and `automation.py` appear in the
cumulative `origin/main...HEAD` diff only from earlier rounds' declared
additive wiring and are untouched here. Per the Integrator's instruction the
branch was **not** rebased onto the new `origin/main` (`2173268`); no merge, no
squash, no new PR — #47 stays the only one. The full suite,
`scripts/premerge.sh`, and CI are the Integrator's to run — **pending in PR**.
Re-checked and unchanged: `calibrated_judges_only` is still `lambda: False` in
`default_worker` and the default adapter is still `_no_adapter`. No model,
paid call, API key, Docker, cloud, Harbor run, policy, profile, or evidence
byte was invoked or modified.