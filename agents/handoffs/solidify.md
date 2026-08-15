Status: building
Last: P3 accepted after migration, three topology-aware full smokes, premerge, and a fresh-clone full smoke against the shared store.
Next: Implement P4 executor timeout, Harbor-label-only orphan cleanup, and capped transient-provider retry/classification.
Blockers: none

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
