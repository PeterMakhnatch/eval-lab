# Node nested build-output leak (000919) → purge-build-caches@5

Owner: NodeLeak. $0 (local Docker `--network none`, registry pulls, host git).
No model calls, no paid compute.

## Diagnosis: why @3/@4 missed 000919

Task `format-code-task-000919` (facebookincubator/idx monorepo, workdir
`/testbed`) bakes `packages/babel-plugin-idx/lib/babel-plugin-idx.js` from
the fixed tree (sha256 `3aa10947…` == post-fix blob; probe-v2 `hit_detail`
with 5 co-located lines; live-image `sha256sum` match). The v4 clean chain
left it in place. Four compounding gaps in `NODE_BUILD_BLOCK` (@3, carried
verbatim into @4):

1. **Top-level-only scan.** The block enumerates `$CWD/lib $CWD/dist
   $CWD/build $CWD/out`. The idx repo keeps outputs per package
   (`packages/*/lib`); the root has none, so `_pbc_bout` was empty and the
   section was a silent no-op.
2. **`git clean --exclude=lib` spares nested outputs.** The `--exclude=lib`
   basename pattern matches `lib` at any depth, so setup's own clean spared
   `packages/babel-plugin-idx/lib/` while the block never looked inside.
3. **Root-only build-script lookup.** The rebuild path reads `compile`/`build`
   from the root `package.json` (`lerna run build`) via `npm run` only;
   per-package build scripts (`babel src --out-dir lib`, repo pins
   `yarn.lock`) were not modeled.
4. **Narrow dir list, root-only usage signals.** `es/`/`esm/`/`cjs/` missing
   (datadog-style `main: cjs/index.js` outputs invisible); usage detection
   (test imports, configs, self-link + entry) ran at root only, so
   per-package `main: lib/…` never counted.

000047 (the @3-closed case) is the contrast: single-package, top-level
ignored `lib/` (59 files) + `main: lib/index.js`, rebuilt via the root
`compile` script. Grading note for 000919: package jest tests require
`./babel-plugin-idx` (src-relative, zero tracked `lib` references, no
workspace self-link), so the baked `lib/` is answer-visible but not
grader-used — safe to delete, which is what @5 does.

## Class size (evidence)

Population: 554 JS/TS tasks (v4 manifest: 388 javascript + 166
typescript). 114 have usable reference fixes: 15 monorepo-`packages` shape,
21 built-output-touching, 45 single-`src`, 33 other. 12 sampled on images
(layout scan + probe/marker check each — the ≥10 required):

| Task | Shape | Baked ignored+untracked outputs | Entry → output | Leak status |
|---|---|---|---|---|
| 000919 | monorepo | `packages/babel-plugin-idx/lib` (1 file, post-fix blob) | main→lib, no self-link | REAL (probe 1 hit) |
| 000435 | monorepo (turbo) | 16× `packages/*/dist` with post-fix compiled logic (`createNewStream`, `millis ?? undefined`) | main+exports→dist | REAL (probe-blind: tsc defeats line patterns) |
| 000170 | monorepo | ~25× `packages/*/dist` with post-fix marker (`groupKeyPattern`, absent from base); `lib/` dirs are tracked `.ts` sources | main→dist, several pkgs scriptless | REAL (probe-blind) |
| 000928 | single root | `dist/` 4545 files (cjs+esm+types) with post-fix markers | main+exports→dist, tests import `../src` | REAL (no clean probe: @4 setup fails earlier at the @2 pnpm leg) |
| 000047 | single root | `lib/` (59 files) + orphan `setup/dist` | main→lib | handled by @3 (probe 0) |
| 000112 | single root | `lib/` (19) + `dist/` (2) | main→lib | handled by @3 (probe 0) |
| 000055 | monorepo | none baked (ignored cjs/esm absent) | main→cjs | none (probe 0) |
| 000063 | monorepo | none ignored (same-name dirs tracked) | — | none (probe 0) |
| 000045 | single root | none ignored (`lib/` tracked, fix committed there) | main→index.js | none (probe 0) |
| 000095 | single root | none ignored (`lib/` tracked) | main→lib | none (baseline probe 0) |
| 000611 | single root | none ignored (`dist`+`lib`+`test/lib` tracked) | main→lib, no scripts | none (baseline probe 0) |
| 000367 | single (nest) | none baked | main→dist | dep-idiom coincidence (probe 2 hits in `node_modules` nestjs deps, not project content; not purge-removable) |

4/12 leak (all nested-output shape). The probe cannot enumerate the class:
compiled-TS leaks defeat line patterns (000435/000170), and 440/554 tasks
have no reference fix. At-risk, unverified on images (fix-path shape, no
probe): monorepo 000271/000776/000867/001550/001576/001672/001763/001901;
built-output 000632/000794/000876/000934/001437/001808/001890/001931/
001932/001933/001934 (+000490/000733/000874/000875/001660/001673/001869
probed-clean but patch touches `lib/`, gate-verified only by probe).

## Fix: purge-build-caches@5

`src/evallab/purge_build_caches.py`: `NODE_BUILD_BLOCK_V5` (candidate
outputs at any depth attributed to the nearest `package.json` scope, orphan
dirs to `$CWD`; `es`/`esm`/`cjs` added; `exports` first-segment entry check;
any-prefix tracked-source references; per-scope then root `compile`/`build`
lookup; one grouped rebuild per scope+script), `TRANSFORM_ID_V5`,
`BUILD_OUTPUT_DIRS_V5`, `shell_block_v5` (@4 bytes + new node section),
`build_setup_sh_v5`, `build_changes_v5`, `derive_purge_build_caches_v5`.
@1–@4 untouched and byte-identical (verified: zero removed lines).
`src/evallab/hardening.py`: registry entry. `tests/…`: 12 fixture tests
(nested delete, nested rebuild, no-script fail-closed, tracked-untouched,
no-package.json no-op, orphan scope, self-link + cjs entry, nested runtime
asset ref, bare-dir require).

Semantics: ignored-and-untracked candidates only (tracked → strip,
non-ignored → git clean); grader-unused → delete; grader-used → rebuild
from base via scope (else root) script, else fail closed. Per-file
neutralization rejected (half-deleted outputs break resolution like full
deletes but keep the leak). Proven in isolation: faker `dist/` (4545 files)
rebuild path fires once for 5 nested candidates, regenerates, verified.

## Validation (@5 = @4 with only the node section swapped in setup)

| Task | Probe clean (setup, control, real hits) | Oracle / nop |
|---|---|---|
| 000919 | 0, positive, 0 (was 1) | oracle 16/16 pass; nop 2 fail / 14 pass |
| 000435 | 0, positive, 0 | labels pre-established (fold gate: re-run) |
| 000170 | 0, positive, 0 | labels pre-established (fold gate: re-run) |
| 000047 | 0, positive, 0 (matches @4) | pre-established (orphan `setup/dist` newly deleted) |
| 000112 / 000055 / 000063 / 000045 | 0, positive, 0 (match @4) | pre-established |

Raw: `~/Developer/eval-lab-results/2026-10-11/node-build-leak/`
(`v5_probe_summary.json`, per-task `stage-v5`/`out-v5`), class baselines in
`../node-build-leak-class/`. Pattern tweak during validation (bare
`require('lib')` coverage) is strictly fail-closed-ward; verdicts rechecked
unaffected (000919 has zero tracked lib refs; all rebuild outcomes only
reinforced).

## Recommendation to V5Finish

Fold @5 into v5 for **000919** (validated end-to-end), **000435 + 000170**
(probe-clean under @5; re-run oracle/nop as the fold gate — grading flows
through `src`, dist is grading-neutral, but not directly executed). Keep @4
for the sampled-clean/no-op tasks (no leak to fix). **000928 needs the pnpm
leg owner first** (setup fails at `@2 found … references in the pnpm store`
before reaching node handling; node section proven in isolation). 21
at-risk tasks above stay `unverified:node-build-leak` until image-checked.
