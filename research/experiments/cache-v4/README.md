# cache-v4 receipt: purge-build-caches@4 disabled-cache tolerance

Date: 2026-10-10 UTC. Owner: CacheV4. Slice spend: $0 local Docker + one
Modal sandbox setup, 3.65 s wall on default resources (setup sha256
`48cc4a0f…c08864fabd`; `PIP_NO_CACHE_DIR=off` confirmed in the 002552
sandbox too). Modal billing rows lag same-day so the posted actual is
still pending — bounded under $0.25 by duration (single-digit cents at
Modal CPU rates).

Raw run output (out of git):
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-10/cache-v4/`
(`task-002552/modal-setup/`, `task-000047/{oracle,nop}/`).
Harbor job dirs under `/tmp/cachev4-jobs/` — scratch, see residual 3.

## Problem (measured by CensusFinish)

On Modal sandboxes pip's cache is disabled
(`python3 -m pip cache dir` → stdout empty, rc=1,
`ERROR: pip cache commands can not function since cache is disabled.`),
so purge-build-caches@2/@3's fail-closed precondition
(`_pbc_dir` empty → fail) aborts setup for every Python task before the
agent phase. Docker unaffected. Neither pip config nor XDG resolution
causes it; `PIP_NO_CACHE_DIR=off` is present (explicitly enabled), so the
fix keys off pip's own report, not env presence.

## Change (this slice)

`src/evallab/purge_build_caches.py`: new `purge-build-caches@4`
(@3 semantics + disabled-cache tolerance; @1–@3 outputs byte-identical,
verified by AST hash against `origin/main`). @4 swaps the pip leg: when
pip reports a disabled cache — or `PIP_NO_CACHE_DIR` parses true
(`1`/`true`/`yes`/`on`; `off`/`false`/`0`/empty mean enabled, matching
pip) — the `pip cache list`/`remove` calls are skipped as not-applicable
with a logged reason, while any on-disk pip cache at the standard
locations (`$PIP_CACHE_DIR` when set, `~/.cache/pip`,
`/root/.cache/pip`, `$XDG_CACHE_HOME/pip`) still gets a fail-closed
project-entry purge. The enabled path is the @2/@3 leg verbatim. The only
other tool-disabled leg (`GOCACHE=off`) already skips; @4 logs the reason.
`src/evallab/hardening.py`: `CACHE_V4_ID` registry entry (+ docstring).
`tests/test_purge_build_caches.py`: 11 @4 tests. `docs/mimo/vals-routes.md`:
@4 section + V5 line.

## Validation

@4 chains built with the real `ChainBuilder` (@4 swapped in via module
attributes; records and packages in `/tmp`, nothing committed):
`strip>purge-build-caches@4>mtime-normalize@2>separate-verifier@3`
(purge-installed skipped with the same per-task reasons as mimo-clean-v2).

Setup-text diff (@4 cache-step vs the tracked @3 cache-step for 002552):
confined to the pip leg + the go-off echo (plus pre-existing strip drift
from `main`, unrelated).

| task | env | setup | oracle | nop | purge outcome vs @3 |
|---|---|---|---|---|---|
| 002552 (Py) | local Docker (Harbor `verify-local`) | ok | 1 | 0 (+ cheat 0/1 cracked, PASS) | same (enabled path; setup-text diff confined to pip leg + go echo) |
| 000047 (TS) | local Docker (in-image, v3 method) | ok, ready=yes | RC=0, 11/11 jest pass | RC=1, 4 failed on genuine `installDocker` assertions (`Expected: 0, Received: 1`) | same: stale `lib/` (`ami.js`+`ami.d.ts` carry the fix in the raw image) rebuilt from base — post-setup `grep -rl installDocker /testbed/lib/` is empty |
| 002552 (Py) | Modal sandbox (full @4 final setup) | rc=0, ready=yes, `pip cache disabled (pip-reported)` logged | n/a | n/a | pip leg not-applicable; no on-disk dirs present |

## Residuals for the parent

1. Chain adoption is the clean-set owner's call (`CACHE_V4_ID` shipped;
   default chain unchanged until mimo-clean-v3 adopts it).
2. Boundary: content-addressed pip `http-v2` bodies are unattributable by
   filename, so the disabled-path on-disk purge matches package files
   (`*.whl`, `*.tar.gz`, `*.zip`, `*.tar.bz2`, `*.egg`); no (b)-row
   pip-cache hit exists to motivate more.
3. `/tmp/cachev4-jobs/` Harbor job dirs are scratch (shared slow daemon);
   rerun under the clean-set receipt if fleet evidence is wanted there.
