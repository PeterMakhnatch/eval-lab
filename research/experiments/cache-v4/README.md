# cache-v4 receipt: purge-build-caches@4 disabled-cache tolerance

Date: 2026-10-10 UTC. Owner: CacheV4. Transform: `purge-build-caches@4`.

## Problem and change

Modal sets `PIP_NO_CACHE_DIR=off`: `python3 -m pip cache dir` returns rc=1,
empty stdout, and “cache commands can not function since cache is disabled.”
Despite its spelling, `off` disables pip's cache: pip's
`_handle_no_cache_dir` validates boolean text but then unconditionally sets
`cache_dir=False`, including `off`, `false`, `0`, and `no`, for backward compatibility.
The @2/@3 empty-directory precondition therefore aborts setup.

@4 treats pip-reported disabled caches or any nonempty `PIP_NO_CACHE_DIR` as
not applicable to `pip cache list/remove`, with an explicit reason in the log.
It still removes and verifies absence of all on-disk pip cache directories at
`$PIP_CACHE_DIR`, `$HOME/.cache/pip`, `/root/.cache/pip`, and
`${XDG_CACHE_HOME:-$HOME/.cache}/pip`. Whole-directory removal is necessary:
content-addressed HTTP bodies cannot be attributed by their filenames. Path
whitespace and symlinks are handled; command failure or surviving directories
abort setup. An unresolvable project name does not excuse an on-disk cache.
Enabled-cache behavior remains @3's project-selective purge verbatim.

Likewise, `GOCACHE=off` logs the disabled reason and removes stale build-cache
directories at the standard HOME/root/XDG locations, while the Go module-cache
leg stays active. Other @3 legs and the @1–@3 implementations are unchanged.
@4 is registered as `CACHE_V4_ID`; the clean-set owner selects the active version.

## Final-revision validation

Packages were built through the real `ChainBuilder`, with the active cache
transform replaced by @4, using
`strip-future-history@1 > purge-build-caches@4 > mtime-normalize@2 > separate-verifier@3`.
Installed-copy purge retains the existing task-specific skip decisions.

| Task | Backend / method | Full setup | Oracle | Nop | Purge outcome relative to @3 |
|---|---|---|---|---|---|
| 002552 (Python) | Docker, Harbor `mimo-clean verify-local` | OK, no trial exception | reward 1 | reward 0; cache-cheat 0/1 cracked, acceptance PASS | Enabled pip path unchanged; existing language/cache purges retained |
| 000047 (TypeScript) | Docker, isolated in-image full healthcheck and upstream grader, no network | rc=0 for both controls | rc=0, 11/11 Jest tests pass | rc=1, 4 genuine `installDocker` assertion failures, 7 pass | `lib/` rebuilt from base; `grep -rl installDocker /testbed/lib/` empty after each setup before oracle patch |
| 002552 (Python) | Modal sandbox, full final-package setup | rc=0, ready=yes | Not run | Not run | Disabled pip logged with `PIP_NO_CACHE_DIR=off`; standard cache directories absent before and after |

Final package digests:

- 002552: `sha256:f9ca1872b1a8d983838d2cc1d69d6d024b5391fa1d3600eadaf0c3cbb6091c4b`.
- 000047: `sha256:7dc2f53504c03dd523bfb3960b5f5cd76c9b767745f4b3d947c8761971a7b2e5`.
- Modal setup: `sha256:fc94d14087e8feda6578729c9ce32139f3cf4cca4d4a7ecd651101fae0d2ca98`.

Focused behavioral tests: **41 passed** (`tests/test_purge_build_caches.py`).
Ruff check and format check passed for the changed Python files. `make docs`
regenerated the repository map and documentation index in dependency order.

## Raw evidence and spend

Final immutable receipts and pinned input packages are outside Git:
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-10/cache-v4-final/`:
`harbor-jobs/` (002552 oracle/nop/cache-cheat), `task-000047/{oracle,nop}/`,
`task-002552/modal-setup/`, `task-packages/`, and `cache-v4-manifest.csv`.
The earlier revision's actual run evidence is retained separately under
`.../2026-10-10/cache-v4/`; the final table above uses the corrected revision.

Local Docker cost: $0. Two CPU-only Modal setup sandboxes used the owned
`cache-v4-repro` app (`ap-yHp9e28NAvvXHnwfKe5TW9`), with measured wall time
3.65 s and 3.8546 s respectively. The final sandbox requested 0.25 CPU,
256 MiB, timeout 120 s, and was terminated in `finally`; no model or GPU ran.
Before the final launch, `evallab spend day --date 2026-10-10` reported
$7.5981 against the standing $20 daily cap (Modal same-day data partial).

Modal's actual posted billing row is **$0.00001153** for this app at
`2026-10-10T07:00:00`; the second sandbox's billing row has not posted yet.
Do not treat that posted amount as a settled total. Measured resource bounds
remain far below this slice's $0.25 ceiling; the billing receipt records the
posted actual and the explicitly unsettled final sandbox separately.
