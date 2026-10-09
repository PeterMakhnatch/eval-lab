# Route 3 receipt: cache census + purge-build-caches@1 (MEASURED)

Date: 2026-10-09 UTC. $0 (local `docker create`/`export` streaming + local
Docker controls, `--network none`).

## Static census: `receipts/cache-census.csv` (n=11 Python tasks, full image exports)

Method: `docker create` (never started) + `docker export | tar -t`,
tallied against 19 cache patterns (script in scratch
`/private/tmp/mimo-night/vals-closure/cache_census.py`). Read-only.

| Task | __pycache__ | .pytest_cache | egg-info | build/lib | editable | dist-info | venv* |
|---|---|---|---|---|---|---|---|
| 000324 | 6751 | 0 | 0 | 0 | 2 | 890 | 22 |
| 000666 | 4975 | 0 | 0 | 0 | 1 | 360 | 20 |
| 000905 | 21884 | 7 | 5 | 0 | 2 | 481 | 22 |
| 001269 | 2270 | 8 | 7 | 13 | 0 | 244 | 20 |
| 001809 | 6214 | 8 | 7 | 0 | 2 | 809 | 20 |
| 002139 | 5671 | 0 | 0 | 0 | 2 | 598 | 22 |
| 002308 | 2471 | 9 | 7 | 82 | 0 | 303 | 20 |
| 002391 | 4683 | 5 | 5 | 4 | 0 | 562 | 12 |
| 002486 | 4866 | 5 | 5 | 4 | 0 | 238 | 10 |
| 002552 | 3344 | 9 | 0 | 0 | 0 | 342 | 20 |
| 002938 | 4583 | 0 | 0 | 0 | 2 | 346 | 20 |

`*`venv hits are all `/usr/lib/python3.x/venv/` (stdlib) — no worktree
`.venv` in any of the 11. `pip_cache`, `npm_cache`, `node_cache`,
`go_build`, `go_mod`, `m2_repo`, `cargo_reg` are 0 everywhere (Python
images; non-Python module caches are CodeHarden's fleet).

Worktree-scoped samples (the shapes that matter): `testbed/miio/__pycache__/`
+ `testbed/.pytest_cache/` (002552), `testbed/responses.egg-info/` +
`testbed/build/lib/responses/` (001269), `testbed/build/lib/` (002308:
82 members), `testbed/...egg-info` + `build/lib` (002391/002486: 4–5
members each — new installed-copy suspects, follow-up for
purge-installed-copies, NOT claimed here). `__editable__.krakenex*` (002938)
points at the task tree.

## Dynamic facts (decide the transform's scope)

- Setup's `git clean -fdx` already removes *untracked* worktree caches:
  post-1.1.0-setup on 002552 shows zero tracked cache files
  (`git ls-files | grep pycache|pytest_cache|egg-info|build/lib` empty) and
  zero survivors (`find` for `__pycache__`/`.pytest_cache`/`*.egg-info`/
  `build/lib/*` empty).
- 002552's site-packages pointer is benign: `python_miio.pth` contains
  `/testbed`, `import miio` resolves to `/testbed/miio/__init__.py` (base);
  the dist-info ships only metadata + console-script wrappers, no fixed
  code copy.

So `purge-build-caches@1` is defense in depth for what `git clean` cannot
reach: *tracked* caches, caches under setup `--exclude` dirs
(`node_modules/.cache`, `.vitest`), with dependency dirs and project copies
explicitly untouched (module: `src/evallab/purge_build_caches.py`).

## Validation (local Docker, `--network none`, variant packages)

| Task | Variant (parent) | Setup | Caches | Nop | Oracle |
|---|---|---|---|---|---|
| 002552 | cache `b575bbadf17b` (strip) | ok | 0 | 0 (1 failed/3 passed) | 1 (4 passed, fix e88159fb) |
| 002402 | cache `31ae924f6cc9` (strip) | ok | 0 | 0 (5 failed) | 1 (5 passed, fix 56f63eb6) |
| 001269 | cache `65a30b9e6bec` (strip) | ok | 0; `build/`+egg-info retained by design | exit 1 (7 failed/1 passed) | n/a (no confirmed history fix) |

All three records marked `validated` with evidence. Found while validating:
the first block draft used `find -delete` with `-prune` (silently fails —
`-delete` implies `-depth`); fixed to `-exec rm -f`, and the three variants
re-derived. Net: the shipped block has been executed, not just reviewed.
