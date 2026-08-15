Status: building
Last: exact-head review found that a safely unresolved partial job still allowed other approved work and the nightly researcher to run. Unresolved `running/` specs now block both paths with `running_specs_unresolved`, preventing overlap with detached/billable Harbor work; Ruff and all 208 tests pass.
Next: Checkpoint the dispatch barrier, obtain exact-head reviewer approval, then restart all repeated/fresh-clone/CI gates before merge.
Blockers: none.

# SOLIDIFY handoff

## Entry evidence

```text
$ gh pr checks 24
lint          pass
profile       pass
test (3.12)  pass
test (3.14)  pass
ty            pass

$ git log -1 --oneline origin/main
0cb1de7 INSPECTOR: add audited repository overview (#24)
```

## Scope

P1 composed smoke; P2 credential-scoped tick; P3 shared Parquet topology;
P4 timeouts, labeled orphan cleanup, and transient provider resilience; P5
four-hour launchd soak followed by event rotation, nightly PostgreSQL backup,
and CLI surface audit. No policy loosening and no billable calls.

## Independent review repair (current candidate)

The executor now inspects completed Harbor jobs for structured transient trial
exceptions even when Harbor itself exits 0. The classifier recognizes Harbor
0.21's `ApiRateLimitError`, `ApiInternalServerError`, and
`ApiOverloadedError`, while generic nonzero agent failures remain fail-closed so
task prompt text cannot manufacture a retry. The doctor bounds its Docker
daemon probe, crash reconciliation settles the final successful reservation,
and the subscription environment preserves only the non-secret custom Claude
Keychain service/account selectors.

```text
$ uv run --no-sync ruff check .
All checks passed!

$ uv run --no-sync pytest -q
........................................................................ [ 36%]
........................................................................ [ 72%]
.......................................................                  [100%]
```

`uv sync --locked` and unqualified `uv run` initially hit `uv`'s macOS
SystemConfiguration panic inside the restricted command sandbox. The pinned
sync succeeds outside that network sandbox; `--no-sync` proves the installed
environment while final premerge will run outside the restricted sandbox.

Second exact-head review additionally proved that Harbor creates a partial
top-level result with `finished_at=null`, and that a provider 5xx can remain a
generic nonzero-agent exception. Completion discovery is now globally strict on
non-null `finished_at`; recovery classifies terminal transient evidence without
ingesting or settling it, and an archive-only interrupted retry moves to
`failed/` for explicit resubmission. The generic classifier accepts only
Harbor's command/output envelope and scans the adapter-output suffix, not task
text. Policy spend and reservation events share a UTC day, independent of the
PostgreSQL session timezone. Scheduled digest Git commands have a fixed timeout
and no terminal input.

```text
$ uv run --no-sync ruff check .
All checks passed!

$ uv run --no-sync pytest -q
........................................................................ [ 34%]
........................................................................ [ 69%]
..............................................................           [100%]

$ uv run --no-sync pytest --collect-only | tail -3
206 tests collected in 0.16s
```

## P1 — composed smoke

Implementation adds `make smoke` for the full local path and `make smoke-ci`
for the deterministic Docker-free subset. The subset uses the same queue,
fixture parser, Parquet projection, invariant checker, and digest renderer; only
Harbor/Docker and PostgreSQL are replaced by bounded seams. `scripts/premerge.sh`
and Python 3.12 CI both run that subset.

Three consecutive full local runs:

```text
$ make smoke
PASS doctor mode=full
PASS submit->tick job=smoke-oracle-b1jh0nmwjgsw trials=1
PASS catalog job_id=53208317-f714-4ee6-ba8e-8236a94d7b5c
PASS parquet job_id=53208317-f714-4ee6-ba8e-8236a94d7b5c
PASS digest path=runs/_smoke/smoke-oracle-b1jh0nmwjgsw/digests/2026-08-14.md
SMOKE PASS both-stores-agree

$ make smoke
PASS doctor mode=full
PASS submit->tick job=smoke-oracle-a56ss9ghc640 trials=1
PASS catalog job_id=0ac2f153-2b2e-4731-b6e3-96cae68b722b
PASS parquet job_id=0ac2f153-2b2e-4731-b6e3-96cae68b722b
PASS digest path=runs/_smoke/smoke-oracle-a56ss9ghc640/digests/2026-08-14.md
SMOKE PASS both-stores-agree

$ make smoke
PASS doctor mode=full
PASS submit->tick job=smoke-oracle-anzvjx9e5aqa trials=1
PASS catalog job_id=1b3acf8d-e019-4f0c-8ed9-e6dbd8ba3d27
PASS parquet job_id=1b3acf8d-e019-4f0c-8ed9-e6dbd8ba3d27
PASS digest path=runs/_smoke/smoke-oracle-anzvjx9e5aqa/digests/2026-08-14.md
SMOKE PASS both-stores-agree
```

## P2 — credential-scoped tick

The executor already deferred per spec; the new symmetric regression pins the
full behavior and the deferral event now carries the affected job name. With
only Claude authentication, Codex alone remains approved/deferred while
Claude, oracle, and no-op dispatch. With only Codex authentication, Claude
alone remains approved/deferred while Codex, oracle, and no-op dispatch. All
credential probes are injected; the tests touch no real auth store.

```text
$ uv run ruff check src/evallab/queue.py tests/test_queue.py
All checks passed!
$ uv run pytest -q tests/test_queue.py
............. [100%]

$ uv run pytest -q tests/test_queue.py -k credential
... [100%]
$ uv run pytest -q tests/test_queue.py -k credential
... [100%]
```

Fresh clone acceptance at committed P2 head `9643499`:

```text
$ uv sync --locked
Installed 41 packages
$ uv run pytest -q tests/test_queue.py -k credential
... [100%]
```

CI-parity gate after the third run:

```text
$ make premerge
All checks passed!
83 passed in 3.93s
PASS doctor mode=docker-free
PASS submit->tick job=smoke-oracle-xfme531wy1b3 trials=1
PASS catalog job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
PASS parquet job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
SMOKE PASS both-stores-agree
Found 33 diagnostics
premerge green: Python 3.12; ty 33 <= 33
```

Fresh clone acceptance at committed P1 head `9144ee1`:

```text
$ git clone --local --branch role/solidify --single-branch ... .worktrees/solidify-fresh-clone
$ uv sync --locked
Installed 41 packages
$ make smoke
PASS doctor mode=full
PASS submit->tick job=smoke-oracle-brvpcy00qeta trials=1
PASS catalog job_id=57de95bf-5175-49b2-8320-10b537c2a730
PASS parquet job_id=57de95bf-5175-49b2-8320-10b537c2a730
PASS digest path=runs/_smoke/smoke-oracle-brvpcy00qeta/digests/2026-08-14.md
SMOKE PASS both-stores-agree
```

## P3 — one Parquet topology

`derived_root_from_environment()` makes the primary checkout's Parquet root
canonical for the primary checkout and all linked worktrees. A relative
`EVALLAB_DERIVED_ROOT` also resolves against that primary checkout; explicit CLI
overrides remain local to the invoking checkout. Queue ingestion, nightly
backfill, manual ingestion/trajectory export, doctor, smoke, GC discovery,
dashboard reads, and LaunchAgent definitions use the same resolver. Full smoke
now requires the global catalog/Parquet invariant, not merely its new job ID.

The migration copied three complete P1 partitions without overwriting any
destination. The deleted P1 fresh clone had already discarded its raw evidence;
after an exact ID/name/path check, its one rebuildable catalog row was removed
(cascade included its derived trial rows). No raw evidence or shared database
was broadly removed.

```text
$ evallab doctor  # from .worktrees/solidify
checkout /Users/petermakhnatch/Developer/eval-lab/.worktrees/solidify
shared /Users/petermakhnatch/Developer/eval-lab
derived /Users/petermakhnatch/Developer/eval-lab/derived/parquet
ok    postgres       PostgreSQL 18.4 ...
ok    catalog-parquet catalog=20 projected=20 exceptions=0 missing=0 extra=0
```

Three consecutive topology-aware full runs:

```text
$ make smoke
PASS submit->tick job=smoke-oracle-z6k9sspnxa06 trials=1
PASS catalog job_id=517e05b2-34c2-436e-830f-a1a2de6f7ca6
PASS parquet job_id=517e05b2-34c2-436e-830f-a1a2de6f7ca6
SMOKE PASS both-stores-agree

$ make smoke
PASS submit->tick job=smoke-oracle-qd5gamv9w2hw trials=1
PASS catalog job_id=44db2c1d-c458-4d21-9487-4333856a145c
PASS parquet job_id=44db2c1d-c458-4d21-9487-4333856a145c
SMOKE PASS both-stores-agree

$ make smoke
PASS submit->tick job=smoke-oracle-131ga7nxxp83 trials=1
PASS catalog job_id=abf8d775-4331-435e-8335-03d2c7036ce7
PASS parquet job_id=abf8d775-4331-435e-8335-03d2c7036ce7
SMOKE PASS both-stores-agree
```

Fresh clone acceptance at committed P3 head `516d3ad`; the ignored raw smoke
evidence was moved into the active worktree before deleting the temporary clone,
so the shared catalog did not gain another orphan:

```text
$ uv sync --locked
Installed 41 packages
$ uv run pytest -q tests/test_paths.py tests/test_smoke.py
.... [100%]
$ EVALLAB_DERIVED_ROOT=/Users/petermakhnatch/Developer/eval-lab/derived/parquet make smoke
PASS submit->tick job=smoke-oracle-zm8cktj1p6zf trials=1
PASS catalog job_id=491f7fc8-c892-472e-8fb6-6d407241ee8b
PASS parquet job_id=491f7fc8-c892-472e-8fb6-6d407241ee8b
SMOKE PASS both-stores-agree
```

Current P3 gate:

```text
$ scripts/premerge.sh
All checks passed!
88 passed in 3.49s
PASS doctor mode=docker-free
SMOKE PASS both-stores-agree
Found 33 diagnostics
premerge green: Python 3.12; ty 33 <= 33
```

## P4 — bounded, scoped, attributable failure handling

Each spec now carries a validated 1–21,600 second per-attempt wall-clock
allowance (default 1,800). Harbor runs in its own process group under an
executor deadline; timeout metadata and logs remain beside the job. Cleanup
requires all of: Harbor's Compose config label, a working directory below the
current task, a Compose project matching a trial-session directory recorded in
the current job, and a container ID absent before launch. It removes only those
exact IDs with `docker rm -f --`; no prune command exists.

Provider 429/5xx evidence normalizes to `transient_harness` and specific queue
reason codes. Two retries maximum use 5/10-second backoff; prior attempts and
per-attempt executor logs are retained. Each billable retry reserves another
full estimate through the unchanged policy gate. Catalog normalization and the
digest separate transient capacity, and quiet-failure counting skips it. All
model-facing subprocess environments now use a non-secret allowlist; the `.env`
loader ignores model API-key names and the Keychain probe sends secret output
directly to `/dev/null` rather than Python.

Three consecutive focused runs (timeout, label scoping, provider
classification, retry cap/backoff, budget reservation, quiet-failure exclusion,
digest taxonomy, and environment filtering):

```text
$ pytest -q tests/test_runner.py tests/test_queue.py tests/test_unattended.py -k 'timeout or transient or provider or orphan or subscription or local_env'
............ [100%]
$ <same command>
............ [100%]
$ <same command>
............ [100%]
```

Three consecutive real Harbor timeouts at five seconds reached container
creation. Docker event names match the job's recorded trial-session directories,
and the post-run inventory contains only the `eval-lab` infrastructure project:

```text
PASS resilience-timeout-control-3 trial_wall_clock_timeout
PASS resilience-timeout-control-4 trial_wall_clock_timeout
PASS resilience-timeout-control-5 trial_wall_clock_timeout

created: event-summary__be596hl__env-main-1
created: event-summary__ie4inhm__env-main-1
created: event-summary__mshy65q__env-main-1

$ docker ps -a --filter label=com.docker.compose.project ...
1bdc828d1ac0 eval-lab .../eval-lab/compose.yaml
4cbac9e731c4 eval-lab .../eval-lab/compose.yaml
```

Three consecutive normal full smokes on committed P4 head `7d82e40`:

```text
PASS job=smoke-oracle-1wke1jcp0f5w job_id=ae9d8be9-cc3d-45c9-80f8-c758442d5cb3 both-stores-agree
PASS job=smoke-oracle-7btbecdknw1m job_id=4b8dd780-bc6d-45f4-a8c3-5351620129c2 both-stores-agree
PASS job=smoke-oracle-etmmprn7pr26 job_id=1c12b25d-a0b3-4828-ae40-c6a0cb743fd7 both-stores-agree
```

Fresh-clone acceptance at `7d82e40`; its cataloged smoke raw evidence was moved
into the active worktree before the temporary clone was deleted:

```text
$ uv sync --locked
Installed 41 packages
$ pytest ... -k 'timeout or transient or provider or orphan or subscription or local_env'
............ [100%]
FRESH PASS trial_wall_clock_timeout
PASS submit->tick job=smoke-oracle-dbqb5hqvftpb trials=1
PASS catalog job_id=f26f07e6-ded8-4002-8346-dfb387188dfa
PASS parquet job_id=f26f07e6-ded8-4002-8346-dfb387188dfa
SMOKE PASS both-stores-agree
```

Current P4 gates:

```text
$ scripts/premerge.sh
All checks passed!
100 passed in 3.61s
SMOKE PASS both-stores-agree
Found 33 diagnostics
premerge green: Python 3.12; ty 33 <= 33

$ evallab doctor
ok    catalog-parquet catalog=29 projected=29 exceptions=0 missing=0 extra=0
```

## P5 — launchd soak

Started from committed head `fbee96e` after a green doctor. The scheduler
captures the shared derived root and points at this worktree. Earliest valid end
is `2026-08-15T00:45:43-0400`.

```text
SOAK_START_LOCAL=2026-08-14T20:45:43-0400
doctor: catalog=29 projected=29 exceptions=0 missing=0 extra=0
LaunchAgent command: cd .../.worktrees/solidify && uv run evallab tick
LaunchAgent EVALLAB_DERIVED_ROOT: .../eval-lab/derived/parquet
runs = 1; last exit code = 0
2026-08-15T00:45:46.706855Z tick_deferred reason=no_approved_specs
runs = 2; last exit code = 0
2026-08-15T01:15:49.952436Z tick_deferred reason=no_approved_specs
runs = 3; last exit code = 0
2026-08-15T01:45:53.118591Z tick_deferred reason=no_approved_specs
runs = 4; last exit code = 0
2026-08-15T02:15:56.374586Z tick_deferred reason=no_approved_specs
runs = 5; last exit code = 0
2026-08-15T02:45:59.191665Z tick_deferred reason=no_approved_specs
runs = 6; last exit code = 0
2026-08-15T03:16:02.455344Z tick_deferred reason=no_approved_specs
```

### Continuations in progress

Event writes now rotate at 10 MiB under a thread and process lock, retain seven
archives, and read oldest-to-newest across application and fleet consumers.
Cross-process writers plus a concurrent reader exercise rotation. The projection
invariant explicitly sees archived exception evidence. Atomic nightly custom
format dumps use `pg_dump` inside the Compose container and atomically publish
one fsynced generation directory with its SHA-256 manifest under the primary
checkout's ignored `backups/postgres/`, and
quarantine before canary dispatch on failure or empty output. Neither the host
command nor Python reads a database password or any model API-key variable.

The continuation described 22 CLI commands, but the rebased parser exposes 27
top-level commands and 38 visible top-level/nested help paths; all 38 are pinned.
The audit caught and repaired an existing contract violation: `trajectories`
used to write PostgreSQL and Parquet even without `--export`; it is now genuinely
read-only by default.

Nightly catches all ordinary backup failures at its fail-closed boundary,
including the subprocess's 600-second `TimeoutExpired`; both timeout and I/O
failure regressions prove that canary dispatch remains zero, a specific
`postgres_backup_failed` reason is appended, and the digest reports the cycle as
quarantined. The latest full suite collects 185 tests.

```text
$ pytest -q tests/test_queue.py tests/test_pipeline.py tests/test_unattended.py tests/test_gc.py
......................................... [100%]

$ pytest -q tests/test_backups.py tests/test_unattended.py tests/test_canary.py tests/test_pipeline.py
........................... [100%]

$ pytest -q tests/test_cli_audit.py
..................................... [100%]

$ evallab summarize research/evidence/runs
event-summary-nop-evidence ... reward=0
event-summary-oracle-evidence ... reward=1

$ evallab trajectories research/evidence/runs
event-summary-nop-evidence ... none
event-summary-oracle-evidence ... none

$ evallab fetch --audit
5 benches, 0 fail

$ evallab gc
gc plan: 0 action(s), 0 skipped, reclaim=0 bytes
```

The first sandboxed real backup attempt failed at Docker-socket access and left
`backups/postgres/` empty, as required by the no-partial-artifact contract.
Docker Desktop was subsequently restarted and the required real acceptance
passed:

```text
$ create_postgres_backup(..., 2026-08-14)
/Users/.../eval-lab/backups/postgres/evallab-2026-08-14.dump
95604

$ shasum -a 256 .../evallab-2026-08-14.dump
84c2998200ff9e6ef4acb41da0d220cd3a52ad9aa9eeef9103857bbd84195e4a
$ stat ...dump ...dump.json
-rw------- 95604 ...dump
-rw-------   280 ...dump.json
$ docker compose ... exec -T postgres pg_restore --list < ...dump
Archive created at 2026-08-15 01:10:33 UTC
dbname: evallab
TOC Entries: 77
Dumped from database version: 18.4
Dumped by pg_dump version: 18.4
```

Three consecutive combined continuation runs at committed code (event
rotation, archived exception invariant, backup atomicity/quarantine, unattended
flow, and complete CLI help inventory) each passed 75 tests. The full suite and
repository contracts now run meaningfully inside linked worktrees: fresh-clone
testing exposed that the old inventory helper excluded every path because the
absolute path contained `.worktrees`. The helper now tests repo-relative parts,
has a non-vacuity assertion. The latest linked worktree passes 145 tests; the
protocol-compliant fresh clone passed 144 at the preceding code head and will
be repeated at the final head after the soak.

```text
$ pytest -q <P5 continuation set>  # repeated three times
........................................................................ [ 97%]
..                                                                       [100%]

$ scripts/premerge.sh
All checks passed!
144 passed in 5.12s
SMOKE PASS both-stores-agree
Found 33 diagnostics
premerge green: Python 3.12; ty 33 <= 33

$ fresh-clone: ruff check . && pytest -q
All checks passed!
........................................................................ [ 50%]
........................................................................ [100%]
$ fresh-clone: python -m evallab.smoke --docker-free
SMOKE PASS both-stores-agree
```

Current-head full local smokes (followed by one full fresh-clone smoke against
the shared primary Parquet root) all passed; the fresh clone's ignored raw job
was moved into this worktree before cleanup. An initial `/private/tmp` clone was
removed after noticing the one-folder protocol; all fresh-clone evidence cited
here was repeated from `.worktrees/solidify-fresh-clone`:

```text
smoke-oracle-8ya566yyqwms  055257d1-83f2-413b-8752-9e91dee799f9  PASS
smoke-oracle-j6stzs3br6te  daae9eb6-0815-46a8-b50a-a61a9e8853bc  PASS
smoke-oracle-a3jes7s4jh2g  e382d304-53bb-4a33-a6bf-281d759b2a23  PASS
fresh smoke-oracle-zzf5fhxxxjzd a28fd5bb-4637-4ce8-98b0-52fde278aa97 PASS
```

Current committed-code repeat (after the final backup timeout
regression) passed the complete premerge gate three consecutive times. Every
run reported Ruff clean, 146 tests passing, the Docker-free composed smoke
invariant, and the pinned 33-diagnostic type ceiling. Three consecutive full
local Oracle smokes then passed against the live PostgreSQL/shared-Parquet
topology:

```text
premerge pass 1: 146 passed; SMOKE PASS both-stores-agree; ty 33 <= 33
premerge pass 2: 146 passed; SMOKE PASS both-stores-agree; ty 33 <= 33
premerge pass 3: 146 passed; SMOKE PASS both-stores-agree; ty 33 <= 33

smoke-oracle-3gcjgjqwyjra e025a979-6bb6-4dab-9bb6-af927c6672ac PASS
smoke-oracle-cs5c5awpfsdy 530bbba3-ca2b-470c-be26-33f7158b5f7b PASS
smoke-oracle-rr3qaach3j4n 7517c242-7154-42d0-a5b2-820a7853563f PASS

$ evallab doctor
ok    catalog-parquet catalog=37 projected=37 exceptions=0 missing=0 extra=0
```

The disposable protocol-compliant clone was recreated after those repetitions
from the then-current branch head `8a5ca30`, synced from the lockfile, and
passed the complete premerge gate. Its full smoke explicitly used the canonical
primary-checkout Parquet root. The one cataloged raw job directory was moved
back into this worktree before the clean clone was removed; doctor then proved
the enlarged global invariant. Upstream subsequently added only archived
mission prompts (`1fc986f`); the 24-commit branch rebased without conflicts.

```text
$ uv sync --locked
Installed 41 packages
$ scripts/premerge.sh
All checks passed!
146 passed in 11.00s
SMOKE PASS both-stores-agree
Found 33 diagnostics
premerge green: Python 3.12; ty 33 <= 33

$ EVALLAB_DERIVED_ROOT=.../eval-lab/derived/parquet make smoke
PASS submit->tick job=smoke-oracle-06jyeb02basb trials=1
PASS catalog job_id=553392e5-1c59-4b00-b86d-e397308c7b75
PASS parquet job_id=553392e5-1c59-4b00-b86d-e397308c7b75
SMOKE PASS both-stores-agree

$ evallab doctor
ok    catalog-parquet catalog=38 projected=38 exceptions=0 missing=0 extra=0
```

The completion audit closed a first-event race in the continuation: event
readers now acquire the shared lock before discovering active/archived
segments, so a reader cannot return an empty snapshot while the first writer
finishes. A deterministic regression fails under the old ordering; the focused
rotation/reader acceptance passed three consecutive times. CLI help now also
states the `trajectories` read-only default accurately. The current branch gate
after both fixes is green:

```text
$ pytest -q tests/test_queue.py -k 'event_log or event_reader'  # 3 times
... [100%]
$ scripts/premerge.sh
All checks passed!
147 passed in 5.64s
SMOKE PASS both-stores-agree
Found 33 diagnostics
premerge green: Python 3.12; ty 33 <= 33
```

The refreshed primary read-only CLI audit passed doctor, summarize,
trajectories, database listing, no-call analysis planning, DSPy calibration
dry-run, fetch integrity audit, GC plan, and a live Harbor registry listing.
The first registry-list attempt was DNS-blocked by the sandbox; the identical
read-only command passed with ordinary network access. No stateful, model, or
cloud action was used.

## Integration note

After tick 5, upstream advanced through TRUTH PR #29 and the path-forward
checkpoint. `git rebase origin/main` stopped while replaying SOLIDIFY's initial
mission row because `agents/ROLES.md` now also contains TRUTH's merged row. The
rebase was immediately aborted; no conflict marker or partial upstream state
remains. The overlap is additive, but the shared-file workflow forbids this role
from resolving another role's conflict. Upstream also adds the `power` and
`report` CLI commands, so the final CLI inventory must be refreshed from 25 to
27 top-level commands after an integrator rebases the branch.

## Independent review and remediation

An independent read-only reviewer rejected the first P1-P5 implementation
despite its green tests. The review found that subscription-routing flags were
stripped, the timeout bounded the whole Harbor process rather than each active
trial, failed retry estimates were not durable, free-form 5xx text could retry
a successful/task failure, backup failure rendered `Quarantined: no`, dump and
manifest publication required two renames, concurrent ticks lacked one queue
owner, fleet status ignored rotated logs, and upstream `report family` defaulted
to worktree-local Parquet. No billable/model/cloud/credential-reading action was
used by the reviewer.

The branch now closes each finding:

- infrastructure-only doctor health leaves Oracle/no-op runnable with zero
  model credentials; Executor still defers each unavailable model agent;
- Codex always receives the non-secret auth-file switch; Claude alone is routed
  through the Keychain OAuth wrapper; ambient API-key and OAuth values are not
  forwarded, and the legacy OAuth-to-API-key alias was removed;
- an executor watchdog tracks active Harbor trial directories independently,
  names the triggering trial, retains an aggregate startup fail-safe, bounds
  Docker/tool/Git subprocesses, and keeps cleanup failure secondary;
- only structured provider-facing trial exceptions on a failed Harbor job can
  trigger 429/5xx retry; successful jobs and verifier/task log text cannot;
- every billable attempt writes an estimated-cost reservation before launch;
  failed reservations survive executor restart and participate in all later
  policy gates, while catalog cost settles the successful attempt;
- a nonblocking process/thread tick lock prevents double claims and reports
  `executor_busy` as a terminal scheduled deferral;
- PostgreSQL backup publishes one fsynced immutable generation directory by one
  atomic rename, reuses a verified same-date generation, and renders any backup
  failure as a quarantine with zero dispatch;
- event rotation now has cross-process writer/reader coverage and fleet status
  reads numbered archives before the active log;
- upstream `report family` reads the shared derived root by default; family and
  card reports render without writing unless an output flag is explicit; and
  the CLI audit pins 27 top-level commands and 38 help paths.

The separate integrator fetched `origin/main` at
`e844456714b821ae62cba1048a5ea2132a5f38f2` and rebased all 32 SOLIDIFY commits.
Its only conflict was the additive role-registry insertion; it preserved TRUTH
and DATA-STRATEGY verbatim and one SOLIDIFY row. Both schema families and all
CLI wiring survived. New head after that rebase was
`cb162eded616c8a250e0ab37878e4f2e73437df7`; the working tree was clean and zero
commits behind.

Focused remediation evidence on the rebased branch:

```text
$ pytest -q tests/test_backups.py tests/test_unattended.py tests/test_runner.py tests/test_queue.py tests/test_repository_contract.py
........................................................................ [ 98%]
.                                                                        [100%]

$ pytest -q tests/test_cli_audit.py tests/test_truth.py
...................................................                      [100%]

$ ruff check . && pytest -q
All checks passed!
........................................................................ [ 38%]
........................................................................ [ 77%]
.........................................                                [100%]
```

This is remediation evidence, not final acceptance: the exact final head still
requires three complete premerge runs, three full local smokes, and a fresh
protocol-compliant clone after the four-hour soak completes.

## Continuation CLI read-only correction

The structural CLI inventory exposed one semantic gap: `report family` and
`report card` were suitable inspection commands but published files by default.
`family_report` was already pure; the card path is now split into pure
`build_eval_card` and the backward-compatible explicit writer
`draft_eval_card`. The CLI renders by default and writes only with
`--output-dir` or `--output`. Operations documentation and regression tests pin
that behavior.

```text
$ ruff check src/evallab/report.py src/evallab/cli.py tests/test_cli_audit.py
All checks passed!
$ pytest -q tests/test_cli_audit.py tests/test_truth.py
....................................................                     [100%]
$ ruff check . && pytest -q
All checks passed!
........................................................................ [ 38%]
........................................................................ [ 77%]
..........................................                               [100%]
$ uvx 'ty@0.0.71' check src/ --output-format=concise
Found 28 diagnostics
```

The type ratchet had five stale diagnostics of slack after upstream removed the
`cohort.py` findings. Both local premerge and GitHub CI now cap ty at 28, and
the engineering guide records the current per-file and per-rule distribution.

```text
$ scripts/premerge.sh
All checks passed!
186 passed in 10.20s
SMOKE PASS both-stores-agree
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 28
```

Live continuation checks also confirm that no legacy worktree-only Parquet job
is absent from the shared store, the configured invariant is exact, and the
existing real PostgreSQL custom-format dump still matches its private manifest.
The new report/power read paths were exercised against the shared store without
creating report files; real tests separately prove explicit publication.

```text
$ comm -23 <local Parquet job ids> <shared Parquet job ids>
# no output
local=3
shared=38
$ evallab doctor
ok    catalog-parquet catalog=38 projected=38 exceptions=0 missing=0 extra=0
$ create_postgres_backup(..., date(2026, 8, 14))
/Users/petermakhnatch/Developer/eval-lab/backups/postgres/evallab-2026-08-14.dump
$ shasum -a 256 .../evallab-2026-08-14.dump
84c2998200ff9e6ef4acb41da0d220cd3a52ad9aa9eeef9103857bbd84195e4a
$ evallab report family event-summary
This family contains 33 trials across 31 jobs.
$ pytest -q tests/test_cli_audit.py tests/test_truth.py
....................................................                     [100%]
```

## P5 soak tick 7

```text
$ date '+%Y-%m-%dT%H:%M:%S%z'
2026-08-14T23:46:29-0400
$ launchctl print gui/$(id -u)/com.petermakhnatch.evallab.tick
runs = 7
last exit code = 0
$ wc -l queue/events.jsonl
7 queue/events.jsonl
$ tail -1 queue/events.jsonl
{"occurred_at":"2026-08-15T03:46:05.714413Z","event":"tick_deferred","actor":"scheduled-tick","reason_code":"no_approved_specs",...}
$ wc -c ~/Library/Logs/evallab/tick.error.log
0
```

The post-rebase primary-action audit was repeated without writes. Read-only or
dry-run commands passed for summarize, trajectories, DSPy split calibration,
analysis planning, trace conversion, benchmark integrity, GC planning, family
reporting, power planning, live PostgreSQL listing, and the live Harbor Hub
registry. The registry needed ordinary network access after the sandboxed run
failed; it listed pinned versions only. No new file appeared under `derived/`,
`queue/`, or `runs/` during this audit.

```text
summarize                 exit=0  oracle reward=1
trajectories              exit=0  1 trial inspected
calibrate --dspy-dry-run  exit=0  optimizer_sees_heldout=false
analyze plan              exit=0  estimated=1 maximum=2 calls (no call made)
trace --dry-run           exit=0  skipped expected control without ATIF
fetch --audit             exit=0  5 benches, 0 fail
fetch --list              exit=0  pinned targets only
gc                        exit=0  0 actions, 0 bytes
db list --limit 3         exit=0  3 catalog rows
```

A full free Oracle preview after this audit also passed the actual
Harbor/Docker/PostgreSQL path. It does not substitute for the exact-head
three-run acceptance after the soak.

```text
$ make smoke
PASS doctor mode=full
PASS submit->tick job=smoke-oracle-mz1gyr2gba2a trials=1
PASS catalog job_id=5553c5c4-3751-4a1c-8de7-ed95d7892ca8
PASS parquet job_id=5553c5c4-3751-4a1c-8de7-ed95d7892ca8
SMOKE PASS both-stores-agree
```

## P5 soak tick 8

```text
$ date '+%Y-%m-%dT%H:%M:%S%z'
2026-08-15T00:16:28-0400
$ launchctl print gui/$(id -u)/com.petermakhnatch.evallab.tick
runs = 8
last exit code = 0
$ wc -l queue/events.jsonl
8 queue/events.jsonl
$ tail -1 queue/events.jsonl
{"occurred_at":"2026-08-15T04:16:09.272379Z","event":"tick_deferred","actor":"scheduled-tick","reason_code":"no_approved_specs",...}
$ wc -c ~/Library/Logs/evallab/tick.error.log
0
```

## P5 final four-hour soak audit

Tick 9 crossed the required boundary without any state repair or injected
work. Every launchd run produced exactly one terminal event. The queue remained
empty, all reason codes matched the empty approved queue, no quarantine event
appeared, and scheduler stderr remained empty.

```text
$ launchctl print gui/$(id -u)/com.petermakhnatch.evallab.tick
runs = 9
last exit code = 0
$ jq -s '<terminal event audit>' queue/events.jsonl
events: 9
first: 2026-08-15T00:45:46.706855Z
last: 2026-08-15T04:46:13.441945Z
actors: scheduled-tick=9
outcomes: tick_deferred=9
reasons: no_approved_specs=9
invalid_terminal: []
quarantines: []
$ duration audit
duration_seconds=14426.735090
duration=4:00:26.735090
SOAK PASS every tick terminal, reasons exact, >=4h
$ wc -c ~/Library/Logs/evallab/tick.error.log
0
$ queue state counts
approved=0 running=0 waiting=0 failed=0
```

## Final-gate midnight defect

The first post-soak premerge run was not counted: at 00:47 EDT, the two backup
failure cases rendered `Quarantined: no` for a historical report date even
though `NightlyResult.quarantined` and the queue event were correct. Digest
filtering used only the event wall-clock date, so a nightly failure occurring
after midnight was omitted from the target report. Queue events already carry
the intended `report_date`; digest attribution now treats that semantic date as
authoritative and falls back to local occurrence date only when it is absent.
The regression pins an Aug 14 report with an Aug 15 failure timestamp so it is
no longer dependent on the wall clock.

```text
$ scripts/premerge.sh
FAILED: 2 backup-failure digest assertions (184 passed)
$ pytest -q tests/test_unattended.py::test_nightly_backup_failure_quarantines_before_dispatch
..                                                                       [100%]
$ pytest -q tests/test_unattended.py tests/test_canary.py tests/test_pipeline.py
...........................                                              [100%]
```

## Corrected-head acceptance

After the midnight fix, the complete premerge gate passed three consecutive
times on `5ae301d27e86c35d2d0ea3bb13560b7c6e8224c8`. Each run included the
Docker-free composed smoke and the tightened type ratchet. Three distinct full
Oracle runs then passed the real Harbor/Docker/PostgreSQL/shared-Parquet path.

```text
premerge pass 1: 186 passed; SMOKE PASS both-stores-agree; ty 28 <= 28
premerge pass 2: 186 passed; SMOKE PASS both-stores-agree; ty 28 <= 28
premerge pass 3: 186 passed; SMOKE PASS both-stores-agree; ty 28 <= 28

full smoke 1: smoke-oracle-12s812a3awm9 1d7afc6e-1862-4799-95b3-8f87f86fe1be PASS
full smoke 2: smoke-oracle-9qb2gx5zyqk0 02b74d8a-4eec-42ed-a755-abd7f0d09ce2 PASS
full smoke 3: smoke-oracle-kfnbsykcg79k d42efe27-d2ca-4a00-95fc-536249ccacd3 PASS
```

A new clone at `.worktrees/solidify-fresh-clone` checked out the same head,
created a new Python 3.12 environment from `uv.lock`, and passed the complete
premerge gate. Its full smoke explicitly targeted the primary checkout's
shared derived root. The cataloged raw job was preserved under this worktree's
`runs/_smoke/` before the disposable clone was removed.

```text
$ git rev-parse HEAD
5ae301d27e86c35d2d0ea3bb13560b7c6e8224c8
$ uv sync --locked
Installed 41 packages
$ scripts/premerge.sh
All checks passed!
186 passed in 14.54s
SMOKE PASS both-stores-agree
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 28
$ EVALLAB_DERIVED_ROOT=.../eval-lab/derived/parquet make smoke
PASS submit->tick job=smoke-oracle-4e6c8477xrrt trials=1
PASS catalog job_id=c1244aeb-2963-49b6-b742-6673e2c57a02
PASS parquet job_id=c1244aeb-2963-49b6-b742-6673e2c57a02
SMOKE PASS both-stores-agree
$ evallab doctor
ok    catalog-parquet catalog=43 projected=43 exceptions=0 missing=0 extra=0
```

## Independent final-head review repair

PR #31 reached five green GitHub checks at `805d45e`, but independent review
correctly rejected merge. If fleet enrichment partially modified the digest and
then raised, `NightlyResult` became quarantined while the already-rendered file
could still say `Quarantined: no`; the failure event was not in the digest's
quarantine set. The fleet section also implemented its own occurrence-date
filter instead of honoring semantic `report_date`, and the first midnight test
used 00:00 UTC, which was still the prior local day in EDT.

The repair now:

- records `digest_enrichment_failed`, rerenders the base digest after the
  event, and only then calls the committer, removing partial enrichment;
- classifies that event as a digest quarantine and exposes its reason;
- shares one semantic report-day predicate between the base digest and fleet;
- pins 05:00 UTC / 01:00 EDT on Aug 15 for an Aug 14 report in both backup and
  fleet boundary tests.

```text
$ ruff check <review-repair files>
All checks passed!
$ pytest -q tests/test_unattended.py -k 'backup_failure or enrichment_failure or semantic_report_date'
....                                                                     [100%]
$ pytest -q tests/test_unattended.py tests/test_canary.py tests/test_pipeline.py
.............................                                            [100%]
```
