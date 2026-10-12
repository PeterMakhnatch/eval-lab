# mimo-clean-v5 census receipt (fleet verify under separate-verifier@8 + purge-installed-copies@2)

Date: 2026-10-11. Owner: V5Finish (continuing GraderV7). Slice cap: **$4.50
fresh provider actuals** (from Main; within Peter's $25 programme approval).
Method: `docs/mimo/verification.md` (+ carry rules below). Runner:
`evallab mimo-census run|report|record-spend` (Modal LIMIT grading cells).
Config: `--timeout-seconds 900`, `--modal-resource-policy limit`, 24-wide
remote fan-out, Modal billing app `mimo-clean-census`
(ap-8b0CQZyeIkELvgpjYWb3rH`). Fix-content (check d): ProbeV2's v2 verdicts
merged (#839, digest-bound to v3/v4 packages); v3/v4 `fail:open-leak`
labels were never copied. This slice runs its own v5 probe-v2 batch on the
final packages (11 E1 + 000919, Step 6) instead of carrying probe verdicts
across the purge@2/package boundary.

## Scope and package identity

Target: `mimo-clean-v5` (chain
`strip-future-history@1>purge-installed-copies@2>mtime-normalize@2>separate-verifier@8>agent-network-none@1`
(python; non-python omits the purge slot); cache slot `@4` fleet-wide with
a 20-task `@5` node-build fold — see Step 6; 2,666 built of 2,698; 564
reference fixes). Manifest `research/experiments/mimo-clean-v5/manifest.csv`
(sha256 `8260f47cc0db8298487bf673f763b8bec276e38a52179bcec9561f36efbd2460`;
2646 rows byte-identical to the @4-chain build `c0399116…` in package bytes
(verify column filled by this census), 20 NODE_FOLD rows on @5).
`results.csv` is the v5 fleet result (exact-digest-bound, never merged across
generations). Per-task evidence: `verify-detail.csv` (all 2,698 rows: grade +
fresh cells + carries + overrides). Samples/quotas/digests: `sampling.json`
(seed `20261011`; ladder-20/ladder-250/sample-40 sets pairwise disjoint and
disjoint from the v4 ladder-100 by design). Raw evidence:
`~/Developer/eval-lab-results/2026-10-11/mimo-clean-v5-census/`
(`census-rows.jsonl`; exact-digest rows only).
Triaged labels: `verify-overrides.csv` (12 rows: 9 triaged + 3 fold-gate
pass) merged over untriaged aggregates at every `report` run; bulk coverage
labels re-applied by the documented fill after `report` (same split as v4).

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

## Step 6 — node-build @5 fold (selective, digest-stability)

Probe-v2 (merged #839) found 12 REAL leaks: 11 Python E1 (closed by purge@2;
Step 2 re-grades them on the final chain) and 000919 (node `lib/`, survives
cache@4). NodeLeak shipped `purge-build-caches@5` (merged #840, recursive
node outputs). A uniform @5 rebuild was measured: 2666/2666 digests churn
(setup-script text only; agent tree identical) — adopting it would
invalidate all ~400 banked cells, over the $4.50 cap. Per parent decision:
selective fold. Canonical v5 = @4 fleet-wide + `NODE_FOLD_TASKS` (20 tasks: 3 REAL + 17 at-risk; 000632/001763 reverted after guard failures)
onto @5, per-row chains honest (`docs/mimo/clean-set.md` states the rule
and the uniform-@5 path for the next rebuild). Fold: 3 REAL (000919,
000435, 000170 — oracle/nop/ladder gates on the @5 digests) + 17 at-risk
(`NODE_FOLD_AT_RISK`, 50-setup guard-gated, labeled
`unverified:node-build-fold`). 000928 → `fail:open-leak:node-build-pnpm`
(excluded from training). v5 probe-v2 run (`fix_content_census batch`, 11 E1
+ 000919 on the final packages, local Docker $0, raw
`~/Developer/eval-lab-results/2026-10-11/mimo-clean-v5-probe/`): 12/12
tasks complete, all with setup rc=0, positive published controls, and
**0 real hits on the clean packages** — E1 closure and 000919's 1→0 both
confirmed on the shipped digests. 000812's clean row carries the known
dist-info/AUTHORS coincidence (byte-identical metadata, no code patterns;
real_hits=0 — clean by v2 rules, as PurgeV2 triaged).

Guard final (decision-relevant tasks all run; 8 random-sample tasks also
run, 20 random remainder skipped as moot after the selective decision):
at-risk 16/17 setup rc=0 ready=yes (001672 rc=1 at the @2 python-pip leg,
matching its known env-broken state — no NEW @5 failure, fold harmless);
000632 rc=1 IN @5 (`rebuild did not
regenerate /testbed/coverage/lcov-report/lib` — coverage-report dir
misclassified as grader-used; graded (1, clean ladder) under @4, so kept
@4 as `unverified:node-build-leak`, finding routed to NodeLeak's lane);
001763 rc=1 IN @5 (`rebuild failed`, with and without network — kept @4
as `unverified:node-build-leak`); 001672 rc=1 at the @2 python-pip leg
(matches its known env-broken state, not @5). 000830 (random, ships @4)
rc=1 at the @2 go leg (missing toolchain in image — first setup evidence;
labeled env-broken). The @5 setup-failure rate where @4 succeeded is 1/10
(000632) — against uniform @5, for the documented selective fold.
Fold gates (batch `v5-foldgates-limit`, all fresh Modal LIMIT on the @5
digests): 000919, 000435, 000170 ALL grade oracle=1, nop=0, ladder=clean
(1.4.0). Combined with probe-v2 (000919 1→0 real hits under @5 per
NodeLeak stage-v5; 000435/000170 0 real hits), the three REAL leaks are
closed end-to-end on the shipped packages.
## Step 4 — ladder 1.4.0 on 250 seeded tasks (190 py / 60 non-py)

245/250 banked (batch `v5-ladder250-limit` incl 51-task retry of remainder +
7 unscored): 238 clean, 7 unscored (001120, 001297, 001948, 002204, 002206,
002408, 002443), 5 persistent timeouts (000126, 000825, 001334, 001507,
002572, no row after 2 strikes at 900/1500 s), ZERO cracked. Non-py mix:
60 sampled (go/js/ts/c++/ruby/java/php/unknown). **Any-crack rule: no
crack observed, no stop triggered.**

## Step 5 — nop top-up on Python tasks without v5/v4-carried nop

250 seeded tasks (batch `v5-noptopup250-limit`): 114 banked (76x0,
31x0-noexec, 7xsetup-fail → env-broken with fresh evidence), 136
unstarted/killed by the tool timeout — no retry (cap; they keep their v4
carries). Timeout storms (1500 s aggregate burn, e.g. 001601) dominate the
$1.32 batch cost.

## Manifest verify (2698 rows; `fill_verify.py` from `results.csv` + overrides)

| verify | count | basis |
|---|---|---|
| pass | 12 | 3 fold gates (probe-0 + (1,0) + ladder clean on @5) + 8 non-ref (fresh nop 0 + ladder-1.4.0 clean) + 001431 (@9 fold, Step 7) |
| fail:open-leak:env-dependency | 5 | triaged overrides, excluded from training |
| fail:open-leak:node-build-pnpm | 1 | 000928, excluded from training |
| env-broken | 11 | 000703 (override) + 7 top-up setup-fail + 000830 guard setup-fail + 2 carried |
| unverified:baseline-unreliable | 1 | 000585 override |
| unverified:pre-existing-failures | 1 | 000081 override |
| unverified:node-build-fold | 17 | @5 fold, guard-gated setups |
| unverified:node-build-leak | 2 | 000632/001763 kept @4 (@5 setup fails closed) |
| unverified:probe-pending | 172 | ref with (nop,oracle) evidence, probe outstanding |
| unverified:ladder-missing | 1729 | bulk of fleet (incl 10 E1 with probe-0 + (0,1)) |
| unverified:ladder-unscored | 7 | B4 ladder cells without scores |
| unverified:nop-missing | 294 | mostly top-up unrun (v4 carries kept) |
| unverified:oracle-missing | 88 | ladder-only or nop-only coverage |
| unverified:noexec | 80 | rewards without execution proof (incl theano E1) |
| unverified:cells-missing | 127 | no cells either version |
| unverified:ladder-1.4.0-incomplete | 115 | v3 12/14-attacks clean carried |
| unverified:timeout | 2 | 2–3 strikes each, no row |
| unverified:ladder-healthcheck / ladder-no-reward | 1 / 1 | carried |
| unverified:v4-leak-ungraded | 1 | 001690 (v4 fail:open-leak never carried, no v5 cells) |
| unverified | 32 | skipped builds |
| fail:grader-hole / fail:oracle-wrong / fail:open-leak | 0 | zero cracked ladders, zero confirmed wrong fixes, zero copied leak labels |

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

Cap $4.50. Spent **$4.1586** (booked == provider cumulative delta on app
`mimo-clean-census`: 7.0782 less 2.9196 baseline, reconciled exactly via a
$0.3814 settlement true-up as open-hour buckets landed). Per-batch actuals
in `spend.jsonl`.

| batch | cells | actual USD |
|---|---|---|
| v5-nop35-limit | 35 nop | 0.2304 |
| v5-sample40-limit | 40x(nop+oracle) | 0.2944 |
| v5-ladder20-limit | 20 ladder | 0.1533 |
| v5-ladder250-limit | 250 ladder incl 51 retry (245 banked) | 1.6851 |
| v5-foldgates-limit | 3x(nop+oracle+ladder) on @5 | 0.0934 |
| v5-noptopup250-limit | 250 nop (114 banked) | 1.3206 |
| probe-12 + guard | $0 local Docker (registry pulls, rmi after) | 0.00 |
| v5-trueup-settlement | open-bucket settlement to provider cumulative | 0.3814 |

## Residual gaps (honest)

- Fix-content probe outstanding for 552/564 ref tasks (12/12 v5-probed:
  11 E1 + 000919, all 0 real hits with positive controls); no unprobed ref
  task can reach `pass`.
- Ladder coverage 265/2666 banked (258 clean, 7 unscored; 5 timeouts); the
  bulk of the fleet stays `unverified:ladder-missing`.
- Nop coverage: 192 fresh v5 nop cells banked (35 + 40 + 114 + 3 gates);
  ungraded tasks keep v4 carries or `nop-missing`/`cells-missing`.
- 001690 (v4 fail:open-leak, V6 keras residual) has no v5 cells and keeps
  `unverified:v4-leak-ungraded`; needs a targeted cell or probe.
- NodeLeak's "21" counts 19 enumerated at-risk tasks (all folded or kept
  with labels); the 2 unaccounted ids were never enumerated — flagged for
  his lane. The coverage-report @5 overreach (000632) also needs his lane's
  follow-up (uniform-@5 path) once Main confirms.
- Carries assume deterministic suites; flaky suites could differ.
- The bulk coverage fill must be re-applied after every `report` run
  (overrides survive automatically; documented above).

## Step 7 — separate-verifier@9 fold for 001431 (2026-10-12, $0 local Docker)

Closes the `unverified:G5-open` prescription Step 1 left for the grader
lane. Owner: Fix001431. Slice cap: **$0** (local Docker/CPU only; no
Modal/Daytona/paid API/GPU).

### Ground truth (the hypothesis was wrong)

GraderV7 prescribed "extend the G5 matcher (dotted/`::` suffix match)"
on the theory that the pristine error case is hook-written (raw-nodeid
classname, no file attr). Capturing 001431's pristine `junit.xml`
locally disproves it: pytest 9.0.3's native junitxml writes the
class-level collection error as `(test.logformats_test,
TestGzipASCFileFormat)` with an `<error>` child and no file attr — a
shape `@8` already exempts (module prefix match; `@8` grades the real
pair 1 once skips are removed). The trial failed on the **fixed run's 4
conditional skips**: pristine collection was interrupted after 41 items,
so the baseline records no skip outcomes at all, and the `@6`
unmatched-skip rule trips on every one of them. Captured junits:
`tests/fixtures/separate_verifier/001431-{baseline,agent}.xml`; raw
`~/Developer/eval-lab-results/2026-10-12/fix001431/junits/`.

### The `@9` fix (widen-only)

`separate-verifier@9` keeps the `@8` orphan loop byte-for-byte and
additionally excuses agent skips inside a *satisfied* errored module
(same file or same dotted module, equality/prefix): first executions of
tests the pristine collection error never ran. Only `ftp_bad` clears,
never sets — every `@8` 1 stays 1 (structural argument in
`docs/mimo/verification.md` `@8`→`@9` carries, pinned on fixtures by
`test_v9_keeps_v8_grades`). Design and anti-cheat analysis:
`docs/mimo/separate-verifier.md` (`@9` section). Ships per-row for
001431 only (`SEPARATE_V9_TASKS`; fleet stays `@8`); the next full
rebuild goes uniform `@9`.

### Before/after (same counters, grader-only change)

| cell | `@8` | `@9` |
|---|---|---|
| oracle (rc=0, cases=138, bad=0, skipped=12) | 0 (`fail-to-pass`) | **1** |
| nop (rc=2, cases=1, bad=1, the collection error) | 0 | 0 |

### Fold gates (fresh local-Docker cells on the `@9` digest `b42df9cb`)

Rebuilt 001431 through the real chain (`SEPARATE_V9_TASKS` fold; package
differs from `@8` only in `tests/test.sh` + the Dockerfile version
label): `derived/task-store/variants/mimo-v2.6-rl__format-code-task-001431/b42df9cbc703`.
`evallab mimo-clean verify-local` on that digest: oracle **1**
(`junit-grade.log`: `rc=0 cases=138 bad=0 skipped=12`), nop **0**
(`rc=2 cases=1 bad=1` with execution evidence), full ladder-1.4.0
**clean** (14/14 attacks executed incl. `tamper_source_skip` +
`tamper_source_skiptest`, combined-trial reward 0). Job namespace wart
(honest): `verify-local` hardcodes the `mimo-clean-v4-` job prefix
(`CLEAN_SET_VERSION`), so the `@9`-digest jobs read
`mimo-clean-v4-001431-{oracle,nop,cheat}` — cosmetic only; digests bind
the evidence. Raw jobs: `~/Developer/eval-lab-results/2026-10-12/fix001431/jobs/`.
Fix-content probe v2 on the `@9` package (same `batch` command as Step
6): setup rc=0, positive published control, **0 real hits**
(4 `other`-route line hits, all in unrelated system tooling —
node-gyp/libstdcxx `__init__.py` files sharing import idioms, no
post-fix blob; triaged coincidence per census rules). Raw:
`~/Developer/eval-lab-results/2026-10-12/fix001431/probe/`.
With (a) nop 0 with execution, (b) oracle 1 with execution, (c) ladder
clean, (d) probe-0 with positive control all on the final digest,
001431's manifest `verify` folds to **pass** (override replaced like the
three `@5` fold-gate passes; `results.csv` gains a `docker`-backend row,
the `@8` row retained as history; `census-rows.jsonl` backs it so a
future `report` reconciles rather than drops it).

### No-regression sample (shipped `@8` packages, `evallab run`, local Docker)

| task | oracle | nop |
|---|---|---|
| 000163 | 1 | 0 |
| 000200 | 1 | 0 |
| 000203 | 1 | 0 |
| 002552 | 1 | 0 |
| 000666 | 1 | 0 |
| 000114 | 1 | 0 |
| 000783 | 1 | 0 |

Raw jobs: `~/Developer/eval-lab-results/2026-10-12/fix001431/jobs-reg/`
(`reg-<task>-<cell>`). These run the shipped `@8` graders; `@9`-safety
follows from the widen-only argument plus unit pins on every shape above
(`test_v9_keeps_v8_grades`, `test_v9_anticheat_shapes_stay_zero`).

### Manifest verify deltas from this step

`pass` 11 → 12 (001431); `unverified:G5-open` 1 → 0 (override replaced).
All other rows untouched. `sampling.json` gains `separate_verifier9_fold`.

### Spend

**$0.00** provider actuals (14 regression + 3 fold-gate trials + 1 probe
batch + image pulls all local Docker; pulled images removed after use).
