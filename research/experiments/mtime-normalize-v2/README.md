# mtime-normalize@2: Modal lazy-layer hardening receipt

Date: 2026-10-09. Owner: MtimeV2. Slice spend: $0 Docker + Modal sandbox
repro (two setup-only sandboxes, seconds each; billing rows lag — recorded
below, cap $0.25).

Raw run output (out of git):
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-09/mtime-normalize-v2/`
(`task-002552/modal-setup/`, `task-000047/{oracle,nop}/`,
Harbor job dirs for 002552/001809 oracle/nop/cheat).

## Problem (measured by FleetCensus)

On Modal `_ModalDirect` (`Image.from_registry`) layers materialize lazily:
after `mtime-normalize@1`'s touch walk, directory mtimes get re-bumped, so
its fail-closed `find -newermt` check fails and setup aborts with
`mtime-normalize@1 found paths newer than the fixed timestamp`. A second
touch+check pass in the same sandbox leaves 0 offenders. Docker (eager
materialization) is unaffected. See `docs/mimo/verification.md` (Modal
block) on branch `mimo-clean-census`.

## Change (this slice)

`src/evallab/mtime_normalize.py`: new `mtime-normalize@2` (`@1` untouched,
existing lineage valid; `@2` supersedes `@1` — a `@1` parent is refused,
like the purge `@2`/`@3` supersedes). The @2 setup block:

1. warms the tree with a read-only `stat` walk (forces lazy layers to
   materialize before the first touch);
2. repeats touch+check until a pass yields 0 offenders, max 3 passes
   (`MAX_PASSES_V2`; the Modal repro showed 1 re-bump then 0 offenders);
3. keeps the @1 `-newermt` fail-closed check, then requires every file and
   directory mtime to *equal* the fixed timestamp (epoch comparison against
   a freshly stamped `mktemp` reference file) — strictly stronger than
   `@1`'s newer-only check, so older-than-fixed mtimes fail too.

The check loop is pipe-free POSIX shell, so `pipefail`/`SIGPIPE` cannot
mask an offender; every fallible step fails closed via `fail`.
Registered in `src/evallab/hardening.py` as `MTIME_V2_ID`. Chain adoption
stays the clean-set owner's call (default chain unchanged).

## Live validation (local Docker, $0)

@2 chains built with the real `ChainBuilder` (@2 swapped in; records and
packages in `/tmp`, nothing committed):
`strip>purge-build-caches@1>mtime-normalize@2>separate-verifier@2`
(purge-installed skipped with the same per-task reasons as v1).
`evallab mimo-clean verify-local` on the @2 finals, local Docker,
acceptance 2/2 pass (job dirs under this receipt's results home):

| task | chain | oracle | nop | cheat |
|---|---|---|---|---|
| 002552 | strip>cache@1>@2>sepv2 | 1 | 0 | 0/1 cracked |
| 001809 | strip>cache@1>@2>sepv2 | 1 | 0 | 0/1 cracked |

000047 (TypeScript, not in the clean-set manifest) via the vals-routes-v3
method — composed `strip>@1>@2>@3>@2` setup derived from the `@3` parent,
in-image grading (setup → fix or not → reset test files → `test.patch` →
`mimo_test_command.sh`):

| task | setup rc | oracle (`npx jest`) | nop |
|---|---|---|---|
| 000047 | 0 (+ `ready`, 0 newer paths, single epoch `946684800`) | RC=0, 11/11 `imagebuilder` tests pass | RC=1 on genuine assertions (`Expected: 0, Received: 1` Docker-component counts; failures name `installDocker`, proving discrimination) |

## Modal repro (paid, under $0.25)

Full 002552 @2 `setup.sh` (setup sha256
`2a73bb43…4551`) in a `from_registry` sandbox (the `_ModalDirect` layer
path where @1 setup-fails):

- `setup_rc = 0`, `/var/lib/mimo/ready = yes`, log tail `setup done`;
- `find /testbed -newermt '2000-01-01'` → 0 paths;
- distinct mtime epochs under `/testbed` → exactly one: `946684800`
  (= 2000-01-01 00:00:00 UTC, the fixed stamp).

Sandbox/app: `mtime-normalize-v2-repro`. Provider actual: two setup-only
sandboxes (~4 s wall each: first run proved rc 0/ready/0-newer, rerun
recorded the single-epoch evidence; result + setup log in
`task-002552/modal-setup/`); Modal billing rows lag same-day, so the
posted actual is still pending — bounded under $0.25 by duration (seconds
on default resources).

## Residuals for the parent

1. Chain adoption is CleanSetV2's call (`MTIME_V2_ID` shipped; default
   chain unchanged until they adopt it).
2. `fix_content_census.compose_clean_setup` still composes `@1`
   (vals-routes-v2/v3 `(b)` rows unaffected); bump to `@2` when the chain
   adopts it (ValsRoutes' call).
3. `ty` baseline: 7 diagnostics in `harbor_terminus.py` /
   `harbor_repeat_verifier.py` pre-exist on `origin/main` (untouched by
   this slice); none in the touched files.
