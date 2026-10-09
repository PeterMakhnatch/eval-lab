# vals-routes-v2 receipt: fix-content census + cache purge @2 ($0)

Date: 2026-10-09 UTC. Spend: $0 (local Docker `--platform linux/amd64
--network none`, 20 registry pulls, host git; no model calls, no paid
compute, no Exa).

## What ran

`src/evallab/fix_content_census.py` (probe + host verdicts) over 33 task
images (21 cached + 20 pulled; Python 19, Go 4, TypeScript 3, JavaScript 2,
Unknown 2, Java 1, Rust 1 — every cached code image plus ≥20 pulls across
languages), each in (a) published setup and (b) clean chain
(strip → purge-installed-copies where applicable → purge-build-caches@2 →
mtime-normalize, composed with the real transform functions).

- `census.csv`: one row per task/mode (66 rows over 33 tasks).
- `run_census.py`: fan-out driver (task → fix map in, staged setups + both
  probes out). Fix map: `oracle_sweep.csv` shas re-verified in-image (14),
  full HAR-191 extractor runs on copied `.git` (9 ok incl. 2 divergent,
  1 patch-no-apply used with caveat), lite-S1 fallback
  (`recover_fix_lite`), manual sources (000792 HAR-168 sha, 001738 test
  commit's source parent, 001198/002486 S2-rejected candidates with
  caveats), 3 truncated histories (no fix anywhere: 000001/000236/000250).
- Per-task probe outputs live in scratch (`/tmp/census/<task>/<mode>/`);
  the CSV is the reviewed record.

## Results

(b) closed 24/32 fix rows (no fix object, no content hit, no mtime
signal); 3 rows have no leaked fix; 5 need dispositions (see
`docs/mimo/vals-routes.md`): 000047 OPEN (ignored `lib/` build output with
fix content, spared by setup's `--exclude=lib` — needs a node build-output
port), the rest documented coincidences (single generic lines in unrelated
third-party packages: 000045 JS idiom, 000158 doc example, 000324/000552
Sphinx/typing boilerplate, 000553/000077 dependency echoes) plus one
inconclusive oracle (002486).

Method fixes the census forced (all in-repo, tested): non-test-only
patterns via staged awk, base-absence subtraction (batched `git grep -o`),
pattern floor 20 (kills 15-char coincidences, measured), git-hidden
record for blind post-setup git checks, probe `bash -n` gate test.

## Code changes (this branch)

- `src/evallab/fix_content_census.py` (new) + `tests/test_fix_content_census.py`
  (15 tests, no Docker).
- `src/evallab/purge_build_caches.py`: `purge-build-caches@2` (superset,
  fail-closed, workspace members, cache-absent skips) + tests (14 tests).
  @1 untouched. `src/evallab/hardening.py`: registry entry.
- `src/evallab/instruction_explicit_rules.py` (new, opt-in, NOT in the
  default chain) + tests (6 tests). Quotes Vals' tested wording exactly.
- `docs/mimo/vals-routes.md` (route-by-route V1–V7/E1 with evidence).

## Fleet-wide census proposal (no spend)

Full 2,698-task × 2-mode census at $0: reuse this receipt's pipeline —
`extract .git` per image (docker create/cp, host extractor) then
`run_census.py` fan-out. Measured ≈2 min/probe; 16-way local ≈ 11 h wall,
~2.3 TB disk headroom confirmed. No paid compute needed; nothing requested.
Recommend the parent schedule it as a $0 overnight batch after the node
build-output port lands, so the fleet table can go fully green.
