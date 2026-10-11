# mimo-clean-v5 census receipt (fleet verify under separate-verifier@8 + purge-installed-copies@2)

Date: 2026-10-11. Owner: V5Finish (continuing GraderV7). Slice cap: **$4.50
fresh provider actuals** (from Main; within Peter's $25 programme approval).
Method: `docs/mimo/verification.md` (+ carry rules below). Runner:
`evallab mimo-census run|report|record-spend` (Modal LIMIT grading cells).
Config: `--timeout-seconds 900`, `--modal-resource-policy limit`, 24-wide
remote fan-out, Modal billing app `mimo-clean-census`
(`ap-8b0CQZyeIkELvgpjYWb3rH`). No fix-content (check d) cells in this slice:
ProbeV2 owns the probe + re-triage (ETA ~2-3h after this slice started);
v3/v4 `fail:open-leak` labels were never copied (ProbeV2 confirmed its 11 E1
verdicts are v3/v4-digest-bound and do not transfer to @2 packages).

## Scope and package identity

Target: `mimo-clean-v5` (chain
`strip-future-history@1>purge-installed-copies@2>mtime-normalize@2>separate-verifier@8>agent-network-none@1`
(python; non-python omits the purge slot); 2,666 built of 2,698; 564
reference fixes). Manifest `research/experiments/mimo-clean-v5/manifest.csv`
(sha256 `c0399116fec3be2e89e44171156914cb53b5f3d30e2e12f5c77738a949759561`;
rebuilt from origin/main `a001da408` byte-identically — determinism proof).
`results.csv` is the v5 fleet result (exact-digest-bound, never merged across
generations). Per-task evidence: `verify-detail.csv` (all 2,698 rows: grade +
fresh cells + carries + overrides). Samples/quotas/digests: `sampling.json`
(seed `20261011`; ladder-20/ladder-250/sample-40 sets pairwise disjoint and
disjoint from the v4 ladder-100 by design). Raw evidence:
`~/Developer/eval-lab-results/2026-10-11/mimo-clean-v5-census/`
(`census-rows.jsonl`; exact-digest rows only).
Triaged labels: `verify-overrides.csv` (9 rows: G5-open, baseline-unreliable,
pre-existing-failures, env-broken, 5x env-dependency) merged over untriaged
aggregates at every `report` run; bulk coverage labels re-applied by the
documented fill after `report` (same split as v4).

## Step 1 — @8+@2 regrade of the 30 triaged tasks (oracle) + nop-30 + env nop-5

Oracle (GraderV7, all fresh Modal LIMIT on the final chain): 26x1,
1x1-noexec (000585 G8), 3xfail:0 (000081 PRE, 000703 ENV, 001431 G5-open).
Per-G: G1 1/1, G2 8/8, G3 1/1, G4 2/2, G5 10/11 (001431 open), G6 3/3 (incl
live terse 000936=1), G7 1/1, G8 1-noexec. Certified live: 000783=1,
001220=1 (dashed fix), 000936=1 (terse fix).

Nop (this slice, 35/35 cells Modal LIMIT, batch `v5-nop35-limit`): 19x0,
16x0-noexec, zero failures/timeouts. Merged pairs: (0,1)x14,
(0-noexec,1)x12, (0,fail:0)x2 (000081 PRE, 001431 G5), (0-noexec,fail:0)x1
(000703 ENV), (0-noexec,1-noexec)x1 (000585 G8), 5 env nop-only.
001431 nop=0 with oracle fail:0 is consistent with G5-open (the fix works;
the grader cannot certify the baseline side).
## Step 2 — no-regression sample 40 (oracle+nop): 11 E1 leak-closure + 29 v4-(0,1)

40/40 tasks banked (80/80 cells Modal LIMIT, batch `v5-sample40-limit`),
zero failures/timeouts. All 29 regression tasks grade (1,0) — zero
@6→@8/purge regressions. All 11 E1 leak-closure tasks pass on the final
chain: 10x(1,0) (000322, 000466, 000497, 000545, 000812, 002371, 002384,
002668, 002790, 002829) plus 000255 theano (1-noexec,0-noexec — base never
imports on py3.12, grading resolves the worktree, as PurgeV2 documented).
The fixes work with the leak removed; E1-closure carries are never needed.

## Step 3 — ladder 1.4.0 on 20 G-shape tasks

20/20 tasks banked (batch `v5-ladder20-limit`), all `clean` under ladder
1.4.0, zero cracked, zero unscored/timeout. Mix: 8 python, 6 javascript,
4 typescript, 2 go. **Any-crack rule: no crack observed, no stop
triggered.**

## Step 4 — ladder 1.4.0 on 250 seeded tasks (190 py / 60 non-py)

TBD. Any-crack rule: TBD.

## Step 5 — nop top-up on Python tasks without v5/v4-carried nop

TBD (budget remainder).

## Manifest verify (2698 rows)

TBD table.

Carry rules (applied ONLY where documented safe): C1 (grader widen-only,
all languages — `docs/mimo/verification.md`: v4 nop 0 carries; v4 oracle 1
carries for non-E1 tasks); C2 (purge equivalence, Python: @2 only rewrites
installed-copy bytes to BASE and setup-script bytes, so agent-visible
code/tests match v4 for tasks with no measured carriers); E1-closure (the 11
E1 tasks never carry — fresh v5 oracle+nop instead; PurgeV2's 19/19 Docker
validation cited as supporting, not labeling, evidence); env-5 never carry
(labeled, excluded). Ladder-1.4.0-clean never carries across versions (setup
+ verifier bytes both changed); tasks with only v4-clean ladders keep a
ladder-missing-family label noting the v4 result. Fix probe outstanding for
every ref task without ProbeV2 verdicts → `unverified:probe-pending`
regardless of controls. All carries are reasoning-backed labels in
`verify-detail.csv`, never merged `results.csv` rows; deterministic suites
assumed (stated residual risk).

## Finance (provider actuals, Modal billing report, fenced per batch)

Cap $4.50. Spent TBD (booked == provider cumulative delta on app
`mimo-clean-census` vs 2.9196 baseline @ ~14:55 UTC; headroom TBD).
Per-batch actuals in `spend.jsonl`. Open-hour buckets may still creep;
tail true-up reconciles to the provider cumulative exactly.

| batch | cells | actual USD |
|---|---|---|
| v5-nop35-limit | 35 nop | 0.2304 |
| TBD | | |

## Residual gaps (honest)

- Fix-content probe outstanding for all 564 ref tasks (ProbeV2 pending);
  no ref task can reach `pass` until then.
- Ladder coverage TBD/2666 after step 4 (+ step 3); the bulk of the fleet
  stays `unverified:ladder-missing`.
- Nop coverage TBD after step 5; ungraded tasks stay `unverified:nop-missing`
  / `cells-missing`.
- Carries assume deterministic suites; flaky suites could differ.
- The bulk coverage fill must be re-applied after every `report` run
  (overrides survive automatically; documented above).
