# vals-routes-v3 receipt: purge-build-caches@3 node build-output port ($0)

Date: 2026-10-09 UTC. Spend: $0 (local Docker `--platform linux/amd64
--network none`, 1 registry pull, host git; no model calls, no paid
compute, no Exa).

Closes vals-routes-v2 residual 1 (000047 `lib/` OPEN).

## What ran

`purge-build-caches@3` (same module, supersedes @2, refuses @1/@2 parents;
chain strip → purge → @3 → mtime) appends node gitignored build-output
handling to the @2 block: each of `lib/` `dist/` `build/` `out/` present
and gitignored is classified grader-used vs grader-unused, deleted, and —
when grader-used — rebuilt from the base tree (`compile` script preferred,
`build` fallback, 900 s cap) with fail-closed verifies. Tracked build
output is versioned source and skipped (strip's base reset covers it).

Dependence signals (any one forces rebuild): worktree-test
`from`/`require` imports through the dir, test-runner configs, the `test`
script, a `node_modules` self-link resolving through a package entry point
inside it, and tracked-source runtime asset references (`git grep` for
`'lib'`/`'../lib/` over tracked `*.ts/*.js`, `package.json` excluded).

- `census-v3.csv`: 10 rows (5 node tasks × published/clean), same schema as
  the v2 census. Chain composed with the real transform functions
  (`compose_clean_setup(..., caches_version=3)`).
- 000047: new full extractor-independent rerun under @3 (same fix
  7749647d, S1). 000254: new HAR-191 extraction (S1, `c33e958f`, "Fix
  accessType resolution for MagdaReference", distance 1, applies on base)
  plus @3 census. 000007/000025/000045: @3 regression reruns (v2 fixes).
- Oracle/nop grading on 000047's @3 setup: clean setup → apply oracle
  patch (or not) → reset test files → apply hidden `test.patch` → run
  `mimo_test_command.sh` (`npx jest test/imagebuilder.test.ts`), same
  semantics as `tests/test.sh`.

## Results

000047 (the v2 OPEN leak) is closed: published `lib/ami.js`+`ami.d.ts`
carry 4+2 fix lines (`{worktree: 2}`, `open_leak=yes`); @3 clean has 0
hits, 0 unreachable, normalized mtimes (`open_leak=no`). The tracked-source
asset grep is what fires (`src/utils.ts`:
`path.join(__dirname, '..', 'lib', 'lambdas', ...)`; worktree tests import
from `../src`, no configs, no self-link) — traced in the setup log
(`_pbc_script=compile`, `timeout 900 npm run compile --prefix /testbed`,
regeneration verified, +19 s). Rebuilt `lib/` contains no `installDocker`.

Grading intact on the rebuilt tree: oracle RC=0 (all jest suites pass);
nop RC=1 failing on genuine assertions (`Expected: 0, Received: 1` on
Docker-component counts). Negative control: the naive delete-only variant
(before asset detection) broke even the oracle with `Cannot find asset at
/testbed/lib/lambdas/aws-image-builder-versioner` — kept as documented
evidence for fail-closed-toward-rebuild, not shipped.

000254 (TerriaJS): fix lives in TRACKED `lib/*.ts` source; @3 skips it by
design, strip's base reset covers it — published 46,150 unreachable →
clean 0/0, setup green. 000007/000025: published open → clean closed.
000045: clean keeps only the 3 v2-adjudicated 1-line coincidences
(`open_leak` stays mechanically `yes`, same as v2's row; docs disposition
unchanged).

## Code changes (this branch)

- `src/evallab/purge_build_caches.py`: `purge-build-caches@3`
  (`TRANSFORM_ID_V3`, `NODE_BUILD_BLOCK`, `shell_block_v3`,
  `build_setup_sh_v3`, `build_changes_v3`,
  `derive_purge_build_caches_v3`). @1/@2 untouched.
- `src/evallab/hardening.py`: `CACHE_V3_ID` registry entry (+ docstring).
- `src/evallab/fix_content_census.py`: `compose_clean_setup(...,
  caches_version=2|3)` (default 2, chain behavior unchanged).
- `tests/test_purge_build_caches.py`: 8 @3 tests (22 total in file:
  supersede/refusal rules, content tokens, `bash -n`, delete, two rebuild
  paths incl. runtime-asset-ref, no-script fail-closed, absent-output skip).
- `docs/mimo/vals-routes.md`: @3 section, 000047 row/footnote closed,
  V5 CLOSED, residuals updated.

## Method fixes the work forced (all in-repo, tested)

- Dependence grep needs `\s*` after `from` (real imports have a space;
  caught by the rebuild fixture, not by review).
- `package.json` `main` alone must not force rebuild (entry points only
  count with a self-link); the `test` script gets its own narrow check.
- `compile` before `build` (a full `projen build` would run the packaged
  test suite mid-setup).

## Fleet-wide census proposal (no spend)

Unchanged from v2: full 2,698-task × 2-mode census at $0 local Docker
(~2 min/probe, 16-way ≈ 11 h wall). With @3 landed, the fleet table can go
fully green including node build-output tasks. No paid compute needed;
nothing requested.
