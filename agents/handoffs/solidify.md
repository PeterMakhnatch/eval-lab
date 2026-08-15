Status: building
Last: P2 credential-scoped tick passed three consecutive injected-probe runs; fresh-clone proof remains.
Next: Commit P2, repeat its regression from a fresh clone, then implement the P3 shared derived-root topology.
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
