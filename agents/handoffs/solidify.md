Status: building
Last: P1 full smoke passed three consecutive oracle runs and premerge passed with the Docker-free composed smoke; fresh-checkout proof remains.
Next: Commit P1, prove it from a fresh checkout, then implement the P2 multi-spec credential-scoping regression test.
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
