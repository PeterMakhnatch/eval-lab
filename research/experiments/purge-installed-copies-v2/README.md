# purge-installed-copies@2: neutralize installed copies in place

Owner: PurgeV2. $0 (local Docker `--network none`, registry pulls, host git).
No model calls, no paid compute. Spend actuals: $0.

## Problem

Probe-v2 (`~/Developer/eval-lab-results/2026-10-11/fix-probe-v2/real-leaks.md`,
11 of 256 reference-fix tasks) shows the post-fix project surviving the v3/v4
clean chain in `site-packages`/`dist-packages`, baked `/testbed/.venv` installs,
`build/lib`, and a stray second checkout (`/testbed/<pkg>` while the workdir is
`/workspace/repo`). `purge-installed-copies@1` is scoped to 2 confirmed tasks
(HAR-194) because its delete+reinstall design fails setup closed elsewhere
(poetry backend, unidentifiable project name/version, PEP 668, bitbake).

## Change (`src/evallab/purge_installed_copies.py`, `purge_container.py`)

`purge-installed-copies@2`, for ALL Python code tasks (no-op when nothing matches):

- **Tops from `git ls-files` at BASE** (no project-name resolution, so poetry
  backends and name-less trees work): package tops are first components of
  tracked `.py` files with a tracked `__init__.py` or ≥2 modules (`src/`
  layouts map to installed names; `doc/docs/examples/scripts/tools/benchmarks`
  never count); single-module tops are tracked root `.py` files
  (e.g. `pyupgrade.py`; `setup.py`/`conftest.py` excluded).
- **Matcher**: any file under a searched root whose path starts at the
  project's own top-level package with ≥2 components (`theano/tensor/opt.py`),
  or a single-module file directly under a site/dist-packages/build root.
  Searched: every interpreter's site roots found (`sys.path`, ≤12 discovered
  interpreters queried, `/usr/local|/usr/lib/python*/`, user site, bounded
  sweeps of `/testbed /workspace /opt /root /tmp /home` plus worktree
  `.venv`/`.pixi`), worktree `build/lib*`, and validated stray checkouts
  (dir named `<top>` under `/testbed|/workspace` holding `__init__.py` or a
  tracked rel). Vendored copies (`setuptools/_vendor/...`) cannot match (top
  must be a direct child of the searched root); `dist-info`/`egg-info` trees
  are never descended; pipx/pip tooling homes are excluded (task-design
  residual, per real-leaks.md).
- **Neutralize, don't delete**: tracked copies overwritten with worktree BASE
  bytes; fix-added source files deleted (tracked *test* files are overwritten —
  installed trees ship them, and excluding them misclassified theano's
  `tests/` as fix-added deletions); adjacent `__pycache__` entries dropped;
  untracked compiled artifacts (`.so`/`.pyd`/...) stay (documented residual).
  Install metadata and other distributions untouched. Every action logged to
  setup stdout (the setup log).
- **Fail-closed verify**: re-scan finds no non-base bytes; then the import
  check — tops that imported *before* (real isolated import, not `find_spec`)
  must still import after. Existence-probing false-failed on theano (its own
  `__init__.py` is Python-2 syntax that never parses on the image interpreter,
  proven on the pristine image), and pre/post import comparison is the sound
  gate: never-imported tops (e.g. venv-only installs under a different
  interpreter) skip with a log line.
- **Chain mechanics**: `@2` refuses double-insert and anchor-less setups;
  a carried `@1` block is excised (3-heredoc shape check) and recorded as
  `superseded_v1` instead of stacking purges. Registered in `hardening.py`
  (`PURGE_V2_ID`, `derive_purge_installed_copies_v2`). `@1` untouched.

## Validation ($0, local Docker)

Validation packages (ledger run package → strip@1 → purge@2 → caches@4 →
mtime@2 → separate-verifier@6 + reference solution; `created_by=
purge-v2-validation`, lineage records gitignored): manifests and drivers in
`/tmp/purge-v2-validate/` (`build.py`, `leakcheck.py`, `trials.py`,
`manifest.csv`); raw trial jobs in
`~/Developer/eval-lab-results/2026-10-11/purge-v2-trials/`.

### Leak closure (11/11)

Per task: pre-setup carrier sha256 reproduces the probe's post-fix bytes;
then the validation package's full `setup.sh` runs in the image (setup rc=0
everywhere — the @2 fail-closed verify incl. the import gate passed); then
every probe byte-identical carrier equals the worktree BASE file:

| Task | Carrier(s) | Before (post-fix) | After (== base) |
|---|---|---|---|
| 000255 theano | `/usr/local/lib/python3.12/dist-packages/theano/tensor/opt.py` | `2bddae5c…` | `e2c78dfb…` == worktree |
| 000322 aiohttp | `/testbed/.venv/.../aiohttp/web_fileresponse.py`, `web_request.py` | recorded shas | == worktree base |
| 000466 pyupgrade | site + `build/lib` `pyupgrade.py` | `e0216e44…` | `f9e18b83…` == worktree |
| 000497 powertools | site `.../utilities/batch/base.py` | recorded | == worktree base |
| 000545 behave | `build/lib/behave/configuration.py` | recorded | == worktree base |
| 000812 oauth | site + build `oauth2_validators.py`, `views/oidc.py` | recorded | == worktree base |
| 002371 pydantic | `build/lib/pydantic/main.py` | recorded | == worktree base |
| 002384 janitor | site + build `janitor/functions/complete.py` | recorded | == worktree base |
| 002668 sktime | site + build `sktime/forecasting/structural.py` | recorded | == worktree base |
Stale pyc for neutralized modules dropped (verified absent-or-changed);
probe filename-sweep coincidences (unrelated `base.py`/`setup.py` pyc, system
AUTHORS files, `dist-info/AUTHORS`) correctly untouched — enforcement is gated
on byte-identity with post-fix, not filename. Setup transcripts (tops, roots,
per-file neutralize lines, import verdicts) in each image run's setup log.
### Oracle/nop + no-regression (19/19 PASS)

Local Docker, model-free oracle/nop agents on the validation packages
(`TRIALS-ALL-PASS` in both batches; raw jobs in
`~/Developer/eval-lab-results/2026-10-11/purge-v2-trials/`; per-cell rewards
in `/tmp/purge-v2-validate/trial-results-{ab,c}.json`). No cheat ladder:
@2 touches neither the verifier nor test files, and fleet cheating belongs
to the v5 census.

| Task | Oracle | Nop | Note |
|---|---|---|---|
| 000255 theano | 1 | 0 | leak task; base never imports on py3.12, grading resolves the worktree |
| 000322 aiohttp | 1 | 0 | leak task; solution patch applies in-trial (probe's drift note was image-base-only) |
| 000466 pyupgrade | 1 | 0 | leak task |
| 000497 powertools | 1 | 0 | leak task |
| 000545 behave | 1 | 0 | leak task |
| 000812 oauth | 1 | 0 | leak task |
| 002371 pydantic | 1 | 0 | leak task |
| 002384 janitor | 1 | 0 | leak task |
| 002668 sktime | 1 | 0 | leak task |
| 002790 pylingual | 1 | 0 | leak task (stray checkout) |
| 002829 tuf | 1 | 0 | leak task |
| 002552 poetry py3.8 | 1 | 0 | @1 fail-closed → sets up and grades under @2 |
| 001269 responses | n/a (no fix) | 0 | @1-carried; @2 supersedes (`superseded_v1=True`) |
| 002308 pre-commit | n/a (no fix) | 0 | @1-carried; @2 supersedes (`superseded_v1=True`) |
| 000666 | 1 | 0 | @1 scope-skip |
| 000114 | 1 | 0 | @1 scope-skip |
| 002391 | 1 | 0 | @1 fail-closed (PEP 668) → sets up under @2 |
| 000792 | n/a (no fix) | 0 | @1 fail-closed (no name) → sets up under @2 |
| 002139 bitbake | 1 | 0 | @1 fail-closed → sets up under @2 |
### Blocker log (resolved)

- Shared Docker VM disk hit 100% (2.0T/2.0T over 531 images) mid-validation:
  `eval-lab-postgres-1` crash-looped (ENOSPC), so Executor trials could not
  start. Freed the only safe unilateral chunk (`docker builder prune`,
  99 GB, no effect on the full disk); Main then evicted ~280 unused
  task images (1.6 TB, keeping the validation set) and trials ran through
  with `ingest=False` (rewards from job dirs; the catalog DB was uninvolved).

## Residuals (honest)

- Untracked compiled artifacts (`.so` etc.) under project dirs stay: deleting
  them could break installs; none of the 11 measured carriers are binaries.
  A compiled fix would survive as machine code (out of probe-v2's text/blob
  scope; flag for a future slice if one is ever measured).
- pipx/pip tooling homes (`/root/.local/share/pipx|pypoetry`, …) excluded:
  task-design residuals per real-leaks.md (001478 geopandas, 001671 escapism,
  001945 scipy, 001971 vendored more-itertools, 002414 pipx poetry).
- Wheels/sdists under `dist/` are not entered (binary artifacts; none of the
  11 carriers live there).
- A distribution literally named `doc`/`tools`/etc. would be missed as a top
  (none measured).
- Setup cost: one `git ls-files`, per-interpreter `sys.path` queries, bounded
  sweeps, and up to two isolated top-level imports (pre + post, only when
  copies changed). Slow-import projects (theano shape) pay import time twice;
  never-importing tops skip the post check.
- `MARKER` (`purge-installed-copies@1` substring) now fires on any
  @2-carrying setup (the embedded container script mentions @1): chain code
  must detect @1 via `V1_BLOCK_START` and @2 via `MARKER_V2` (block-start
  line), never bare-substring. Noted for the v5 chain owner.

## For the v5 chain owner (GraderV7)

Replace `purge-installed-copies@1`/CONFIRMED_PURGE scoping with
`PURGE_V2_ID` + `derive_purge_installed_copies_v2` for every Python row
(no scope checks, no fail-closed skips — @2 no-ops safely): drop the
`setup_carries(current, PURGE_MARKER)` branch (it false-positives on @2
setups, see above), and record `PURGE_V2_ID` in `PYTHON_CHAIN`.
