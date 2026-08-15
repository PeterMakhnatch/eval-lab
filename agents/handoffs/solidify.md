Status: building
Last: P5 continuations committed: lock-safe bounded event rotation, atomic pre-dispatch nightly pg_dump, 25-command/34-path help audit, and read-only trajectories default.
Next: Keep launchd on this worktree through 2026-08-15T00:45:43-0400; finish three-run/fresh-clone continuation acceptance, real pg_dump validation, and the final CLI/runtime audit.
Blockers: The managed shell currently denies Docker-socket/PostgreSQL connections; approved retries returned no process result. launchd itself remains healthy and unsandboxed.

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
```

### Continuations in progress

Event writes now rotate at 10 MiB under a thread and process lock, retain seven
archives, and read oldest-to-newest across every consumer. The projection
invariant explicitly sees archived exception evidence. Atomic nightly custom
format dumps use `pg_dump` inside the Compose container, write a SHA-256
manifest under the primary checkout's ignored `backups/postgres/`, and
quarantine before canary dispatch on failure or empty output. Neither the host
command nor Python reads a database password or any model API-key variable.

The continuation described 22 CLI commands, but the current parser exposes 25
top-level commands and 34 visible top-level/nested help paths; all 34 are pinned.
The audit caught and repaired an existing contract violation: `trajectories`
used to write PostgreSQL and Parquet even without `--export`; it is now genuinely
read-only by default.

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
This does not count as the required real backup acceptance; retry it when the
execution boundary permits Docker.
