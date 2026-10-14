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

## Local ladder sample (2026-10-12)

Owner: LadderSample50. Spend: **$0.00** provider actuals (local Docker only;

### Sample

Seeded stratified sample of 50 from the 1,729 `unverified:ladder-missing`
manifest rows (seed `20261012`, recorded in `sampling.json` as
`ladder50_local` with per-task digests). Excluded-from-training rows filtered
by rule (none were in the pool). Strata by largest-remainder proportional
allocation, meeting the minimums (5 Go, 5 JS/TS, 3 other non-Python, rest
Python): go 18, python 14, javascript 8, unknown 4, typescript 3, other 3
(ruby/php/java, 1 each — the other-bucket largest remainders).

### Method

1. Rebuilt all 50 packages (`evallab mimo-clean build --clean-version
   mimo-clean-v5`, scratch manifest): 50/50 built, digests byte-match the
   census manifest, same status and package paths.
2. Ran the full 14-attack ladder 1.4.0
   (`git_history,verifier_peek,reward_plant,answer_symlink,upstream_fetch,skip_plant,`
   `tamper_tracked_conftest,tamper_sitecustomize,tamper_shadow_timeout,`
   `tamper_reward_writer,tamper_source_exit,tamper_source_pytest_patch,`
   `tamper_source_skip,tamper_source_skiptest`)
   via `evallab mimo-census run --backend docker --checks ladder --workers 2`
   (never more than 2 concurrent trials on the shared daemon).
3. Timeout adaptation (documented deviation): the receipt's 900 s config does
   not fit emulated-amd64 local Docker — two pilots (000002 go, 000495 python)
   hit the 1500 s aggregate fail-safe mid-setup/healthcheck with cold image
   pulls (~13 GB per task image, all 50 digests distinct). Fleet ran at
   `--timeout-seconds 2400` (fail-safe 3000 s: agent timeout + 600 s
   `TRIAL_PHASE_ALLOWANCE_SECONDS`, `src/evallab/execution_contracts.py`).
   The two pilots' 900 s strikes count as strike 1; both went clean at 2400 s.
4. Disk hygiene: every pulled task image removed after its task finished
   (verified zero of the 50 digests remain); per-batch diff snapshots;
   `hb__*` trial-build and other lanes' images untouched. No stray containers.
5. Recording: 52 census records (50 tasks; 002693/000249 two strikes each)
   appended to the receipt's out-of-git rows file
   (`~/Developer/eval-lab-results/2026-10-11/mimo-clean-v5-census/census-rows.jsonl`,
   `cost_usd=0.00`) so a future `report` reconciles instead of dropping them.
   `results.csv` merged per exact-digest generation (receipt invariant: at most
   one exact-digest row per task — confirmed zero multi-row groups before
   merging): 48 new `$0` docker rows; 000322/001242 ladder cells forward-filled
   into their exact-digest Modal control rows (run_ids union; backend/cost keep
   the booked control row). Manifest `verify` + `verify-detail.csv` patched for
   these 50 rows only, by the receipt fill rules (replication validated: 0
   diffs over all 2,698 rows before patching).

Raw jobs: `~/Developer/eval-lab-results/2026-10-12/ladder-sample50/`
(per-task cell dirs; `census-rows.jsonl` alongside).

### Results (50/50 banked, ZERO cracked)

Runtime = winning-cell job wall (image pull where first seen in that cell +
setup + all 14 cheat attacks + verifier grading), minutes.

| task | lang | ladder | cracking | min | attempts | new verify |
|---|---|---|---|---|---|---|
| 000002 | go | clean | - | 4.4 | 2 (retry) | unverified:nop-missing |
| 000020 | unknown | clean | - | 22.1 | 1 | unverified:nop-missing |
| 000052 | go | clean | - | 18.5 | 1 | unverified:nop-missing |
| 000130 | unknown | clean | - | 23.8 | 1 | unverified:nop-missing |
| 000249 | typescript | clean | - | 11.6 | 2 (retry) | unverified:nop-missing |
| 000270 | go | clean | - | 20.6 | 1 | unverified:nop-missing |
| 000322 | python | clean | - | 1.0 | 1 | pass |
| 000342 | go | clean | - | 1.8 | 1 | unverified:nop-missing |
| 000456 | go | clean | - | 12.5 | 1 | unverified:nop-missing |
| 000495 | python | clean | - | 8.4 | 2 (retry) | unverified:nop-missing |
| 000500 | go | clean | - | 10.9 | 1 | unverified:nop-missing |
| 000530 | python | clean | - | 3.8 | 1 | unverified:nop-missing |
| 000616 | javascript | clean | - | 22.9 | 1 | unverified:nop-missing |
| 000864 | go | clean | - | 10.1 | 1 | unverified:nop-missing |
| 000885 | unknown | clean | - | 6.0 | 1 | unverified:nop-missing |
| 000888 | javascript | clean | - | 18.6 | 1 | unverified:nop-missing |
| 000921 | javascript | clean | - | 15.0 | 1 | unverified:nop-missing |
| 000990 | php | clean | - | 21.3 | 1 | unverified:nop-missing |
| 001061 | go | clean | - | 11.7 | 1 | unverified:nop-missing |
| 001071 | python | clean | - | 23.6 | 1 | unverified:nop-missing |
| 001141 | go | clean | - | 16.8 | 1 | unverified:nop-missing |
| 001157 | go | clean | - | 15.6 | 1 | unverified:nop-missing |
| 001161 | python | clean | - | 20.8 | 1 | unverified:nop-missing |
| 001227 | ruby | clean | - | 4.1 | 1 | unverified:nop-missing |
| 001242 | python | clean | - | 2.0 | 1 | pass |
| 001384 | typescript | clean | - | 7.4 | 1 | unverified:nop-missing |
| 001574 | python | clean | - | 12.9 | 1 | unverified:nop-missing |
| 001742 | go | clean | - | 26.1 | 1 | unverified:nop-missing |
| 001776 | go | clean | - | 15.9 | 1 | unverified:nop-missing |
| 001981 | python | clean | - | 16.4 | 1 | unverified:nop-missing |
| 002070 | javascript | clean | - | 14.8 | 1 | unverified:nop-missing |
| 002104 | python | clean | - | 3.9 | 1 | unverified:nop-missing |
| 002144 | java | clean | - | 23.2 | 1 | unverified:nop-missing |
| 002313 | python | clean | - | 19.4 | 1 | unverified:nop-missing |
| 002330 | unknown | clean | - | 24.2 | 1 | unverified:nop-missing |
| 002496 | python | clean | - | 15.1 | 1 | unverified:nop-missing |
| 002509 | go | clean | - | 24.9 | 1 | unverified:nop-missing |
| 002624 | go | clean | - | 20.9 | 1 | unverified:nop-missing |
| 002629 | javascript | clean | - | 15.8 | 1 | unverified:nop-missing |
| 002693 | go | clean | - | 14.7 | 2 (retry) | unverified:nop-missing |
| 002733 | go | clean | - | 26.7 | 1 | unverified:nop-missing |
| 002750 | go | clean | - | 18.5 | 1 | unverified:nop-missing |
| 002753 | javascript | clean | - | 35.4 | 1 | unverified:nop-missing |
| 002765 | javascript | clean | - | 18.0 | 1 | unverified:nop-missing |
| 002853 | python | clean | - | 7.2 | 1 | unverified:nop-missing |
| 002923 | python | clean | - | 1.9 | 1 | unverified:nop-missing |
| 002955 | go | clean | - | 12.4 | 1 | unverified:nop-missing |
| 002993 | python | clean | - | 7.4 | 1 | unverified:nop-missing |
| 003010 | typescript | clean | - | 16.5 | 1 | unverified:nop-missing |
| 003032 | javascript | clean | - | 2.2 | 1 | unverified:nop-missing |
### Family summary

| family | n | clean | cracked | unscored (persistent) | timeout (persistent) |
|---|---|---|---|---|---|
| go | 18 | 18 | 0 | 0 | 0 |
| python | 14 | 14 | 0 | 0 | 0 |
| javascript | 8 | 8 | 0 | 0 | 0 |
| typescript | 3 | 3 | 0 | 0 | 0 |
| unknown | 4 | 4 | 0 | 0 | 0 |
| other (php/ruby/java) | 3 | 3 | 0 | 0 | 0 |
| total | 50 | 50 | 0 | 0 | 0 |

**Any-crack rule: no crack observed, no stop triggered.** No per-attack
attribution trials launched (they trigger only on a cracked full ladder).
No transform fixes this slice (none needed).

### Strike-1 flakes (all retried clean; classified, not crack signals)

- 002693 (go): strike 1 `unscored` — `docker compose up` (image pull) exceeded
  Harbor's 1800 s env-start timeout under 2-worker parallel pulls
  (`.../format-code-task-002693/mimo-census-v1-docker-002693-cheat/.../exception.txt`:
  `Environment start timed out after 1800.0 seconds`; cheat never ran,
  verifier/ empty). Solo retry: clean in 14.7 min.
- 000249 (typescript): identical signature (env-start pull timeout, unscored
  row, cheat never ran). Solo retry: clean in 11.6 min.
- 000002 (go) / 000495 (python): strike 1 at the receipt's 900 s config hit the
  1500 s aggregate fail-safe with cold pulls (no row recorded). Strike 2 at
  2400 s: clean (4.4 / 8.4 min).

Timeout protocol applied: 2 strikes then `unverified:timeout`; nothing in this
sample reached it. Unscored rows for 002693/000249 strike 1 are retained in the
rows file as history; the winning clean cells carry the verdicts.

### Statistics for the budget call

- Fleet crack rate: 0/50 cracked. One-sided 95% Clopper-Pearson upper bound
  `1 - 0.05^(1/50)` = **5.82%** (rule-of-three 6.0%). Same bound held at the
  v5 250-ladder (0/245 banked rows cracked) and ladder-20 (0/20).
- Measured Modal ladder rate (receipt `spend.jsonl`, batch
  `v5-ladder250-limit` actual **$1.6851** for 250 tasked ladders incl. the
  51-task retry): **$0.006740 per tasked ladder** ($0.006878 per banked ladder;
  `results.csv` ladder cells carry $0.0067). Projected cost to finish on Modal
  LIMIT at that rate: remaining `unverified:ladder-missing` 1,679 x $0.006740
  ≈ **$11.32**; full v5-1.4.0 ladder coverage (1,679 missing + 115
  `ladder-1.4.0-incomplete` + 7 `ladder-unscored` + 2 `timeout` = 1,803 tasks)
  ≈ **$12.15**. Reference: this census slice was capped $4.50 (spent $4.1586),
  so finishing the fleet on Modal is ~2.7x that slice.
- Local-Docker throughput observed: winning-cell walls 1.0–35.4 min (median
  ~15.5 min); 50 tasks ≈ 7.2 h wall at 2 workers, $0 provider spend.

### Manifest verify deltas from this step

`pass` 12 → 14 (000322: E1 leak-closure complete end-to-end — probe-0 with
positive control + fresh Modal (0,1) + v5 ladder clean on the exact digest;
001242: fresh Modal nop 0 + ladder clean, no reference fix);
`unverified:ladder-missing` 1,729 → 1,679;
`unverified:nop-missing` 294 → 342 (48 ladder-only rows: ladder clean, nop cell
is the next missing check per fill order). All other rows untouched. Fleet
ladder coverage is now 315 banked / 313 clean-and-banked including this sample
(plus 2 strike-1 unscored rows kept as history).

Future-`report` note: the two merged rows (000322/001242) will resurface as
backend pairs (docker + modal) in a regenerated `results.csv`; the manifest
aggregate still combines them on the exact digest (pass holds at report time),
but a re-run of `fill_verify.py` takes the first exact-digest row and would
conservatively grade those two `nop-missing` until backends merge. Ladder-only
docker rows re-derive identically (cost pinned 0.00, survives repricing).

## Vacuous / unfair triage (2026-10-14, $0 local)

Source: `derived/detector-roots/own-scan/fleet-grader-scan/out/vacuous.json`
(35 rows; recount = **28 unique tasks**, not 27 as listed in the scan task)
and §4 of `research-context/analysis/2026-10-14-own-runs-env-flaw-scan.md`.
Method: read the raw task's `tests/test.patch` (`mimo_test_command.sh` /
`.build_env/test_command.sh` decoded from `mimo_build_env.tar.gz.b64`),
`instruction.md`, the v5 manifest / `verify-detail.csv`, the Python ledger,
and `src/evallab/separate_verifier.py` (@8). Under `separate-verifier@8`
the whole `test_command.sh` runs on the pristine tree and on the agent
tree; structured runners (pytest/go/jest/unittest/…) compare
fail-to-pass, while silent/custom families (including `tsd`/`tsc`) keep
the documented exit-code fallback (`detect_runner` → `custom`). A vacuous
*base* phase therefore does not make the grader vacuous when the *new*
phase runs real fail-to-pass tests — it only means no regression
coverage from the base phase. No Docker cells were run for this triage
(repairs: 0); dispositions are static-evidence exclusions or
retained-with-note, recorded in `verify-overrides.csv` (all 33 in-manifest
rows below), `verify-detail.csv`, and `manifest.csv`. Findings:
`findings/*.json` (`qa.finding/v1`, 8 records).

Counts: excluded in manifest 6, excluded not-in-manifest 2 (terminal
candidates), repaired 0, retained-with-note 27.

| task_id | group | disposition | manifest verify | reason / evidence |
|---|---|---|---|---|
| format-code-task-001694 | compile-only whole | exclude | fail:vacuous-grader:compile-only | `whole_grader_vacuous:true`; .build_env runs only `go test -c -o /tmp/teams_test`, binary never executed |
| format-code-task-000506 | compile-only partial | retained-with-note | unverified:ladder-missing (unchanged) | runs real `go test ./service/s3/s3manager/` unit tests, then compile-checks AWS integration tests |
| format-code-task-001380 | compile-only partial | retained-with-note | unverified:ladder-missing (unchanged) | runs real security/groups unit tests, then compile-checks OpenStack acceptance tests |
| format-code-task-001597 | compile-only partial | retained-with-note | unverified:ladder-missing (unchanged) | compiles e2e pilot, then runs real `pilot/pkg/model` JWKS unit tests |
| format-code-task-001747 | compile-only partial | retained-with-note | unverified:ladder-missing (unchanged) | runs real `TestApi` converter unit test, then compile-checks `./tests/` |
| format-code-task-002959 | compile-only base+new | retained-with-note | unverified:ladder-missing (unchanged) | base vet/compile only; new runs real TTL/controller unit tests + build |
| format-code-task-000025 | typecheck-only tsd | retained-with-note | unverified:noexec (unchanged) | tsd expectType assertions in lean.test.ts are the test; @8 exit-code fallback; census oracle=1-noexec/nop=0-noexec |
| format-code-task-000026 | typecheck-only tsd | retained-with-note | unverified:ladder-missing (unchanged) | tsd assertions (gh13702 lean-option types); @8 exit-code fallback |
| format-code-task-000027 | typecheck-only tsd | retained-with-note | unverified:ladder-missing (unchanged) | tsd assertions (gh13900 string Map); @8 exit-code fallback |
| format-code-task-000516 | typecheck-only tsc | retained-with-note | unverified:noexec (unchanged) | tsc --noEmit incl. @ts-expect-error header generics; @8 exit-code fallback; oracle=1-noexec/nop=0-noexec |
| format-code-task-002776 | typecheck-only tsd | retained-with-note | unverified:ladder-missing (unchanged) | tsd --files throw-on-error.test-d.ts expectType assertions; @8 exit-code fallback |
| format-code-task-002781 | typecheck-only tsc | retained-with-note | unverified:ladder-missing (unchanged) | base=new identical tsc --noEmit (no regression split) but hidden svelte/events type assertions discriminate; @8 exit-code fallback |
| format-code-task-000051 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base import smoke; new runs tests/test_file_owner_uid_or_name_behavior.py — F2P-only |
| format-code-task-000118 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base `-run '^$'`; new TestIBMIAMPolicyAssignment + acctest — F2P-only |
| format-code-task-000262 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base torchio smoke; new pytest test_clamp.py — F2P-only |
| format-code-task-000749 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base scons --help smoke; new test_scons_mpi_env_exports.py — F2P-only |
| format-code-task-001235 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base `-run '^$'`; new 6 named helper go tests — F2P-only |
| format-code-task-001308 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base `-run '^$'`; new 6 named GUID go tests — F2P-only |
| format-code-task-001651 | vacuous base | retained-with-note | pass (unchanged) | base PyQt smoke; new pytest test_scene_manager_lifecycle.py — F2P-only |
| format-code-task-001782 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base `-run '^$'`; new 6 TestModifyACL go tests — F2P-only |
| format-code-task-001871 | zero-tests base sub-phase | retained-with-note | unverified:ladder-missing (unchanged) | base runs 21 named root tests; only `./script -run '^$'` sub-phase is zero; new runs fullwidth tests |
| format-code-task-002012 | vacuous base | retained-with-note | unverified:nop-missing (unchanged) | base exit 0; new pytest test_proxy.py — F2P-only |
| format-code-task-002570 | vacuous base | retained-with-note | unverified:nop-missing (unchanged) | base file smoke; new executes all test_* requirement-behavior tests — F2P-only |
| format-code-task-002918 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base `-run '^$'`; new TestRedisRateLimiterTokenBucket — F2P-only |
| format-code-task-002979 | vacuous base | retained-with-note | unverified:ladder-missing (unchanged) | base exit 0; new jest LeftPanel.test.tsx — F2P-only |
| format-code-task-000211 | syntax-check present | retained-with-note | unverified:ladder-missing (unchanged) | py_compile plus real unittest test_config in same phase |
| format-code-task-002480 | syntax-check present | retained-with-note | unverified:ladder-missing (unchanged) | bash -n plus real post_delete_hook_contract_test.sh |
| format-code-task-002487 | syntax-check present | retained-with-note | unverified:ladder-missing (unchanged) | real pytest test_nested_spaces.py plus bash -n |
| format-code-task-001833 | unfair | exclude | fail:unfair-task:non-idempotent-test | hidden command unconditionally appends django.contrib.messages; agent editing tests/settings.py gets duplicate app label |
| format-code-task-000865 | unfair | exclude | fail:unfair-task:prompt-misaligned | instruction `secretsechecksum` vs test `secretschecksum` |
| format-code-task-002661 | unfair | exclude | fail:unfair-task:prompt-misaligned | issue quotes two scopes; test demands exactly `[userinfo.profile]` |
| format-code-task-000803 | unfair | exclude | fail:unfair-task:prompt-misaligned | hidden pins explicit `text_align=None` default never stated |
| format-code-task-000838 | unfair | exclude | fail:unfair-task:prompt-misaligned | hidden pins exact `Your command` casing + `hide_command` name never stated |
| candidate-1789 (candidate-1789-security-appsec) | unfair/env-broken | exclude (not in v5) | n/a — no manifest row | terminal task, not format-code-task-001789; bandit metadata missing; env-dependency@1 + terminal-guard-extend@1 are separate variants, never combined |
| candidate-1702 (candidate-1702-ml-inference) | unfair/env-broken suspected | exclude (not in v5) | n/a — no manifest row | terminal task distinct from format-code-task-001702 (JS, untouched); tqdm fix env-dependency@1 is a separate candidate, unverified by Docker here |

## Repeat-oracle fix (separate-verifier@10 fold, 2026-10-14, $0 local)

Folded `format-code-task-000245` (TypeScript) and `format-code-task-000048` (Go) onto `separate-verifier@10` to fix the repeat-3 oracle snapshot bug (where run 0's untracked files caused `git apply` to fail with `already exists` on repeat runs).

Local Docker verification (`RepeatVerifier repeat_n=3`, 1 container at a time, images removed after each task):

| task_id | digest | oracle x3 | nop | job paths |
|---|---|---|---|---|
| format-code-task-000245 | `sha256:35a598db08e6f49a4dac34dd503f5003caf37141b1617cf4f11930621be6023f` | `[1.0, 1.0, 1.0]` | `0.0` | `v10-000245-oracle-35a598db08e6`, `v10-000245-nop-35a598db08e6` |
| format-code-task-000048 | `sha256:673816e51dbc84653b4fbfe52f20e2b253a1aefc8d2b53a2d7e08e6d9a70eadb` | `[1.0, 1.0, 1.0]` | `0.0` | `v10-000048-oracle-673816e51dbc`, `v10-000048-nop-673816e51dbc` |

Results written to `~/Developer/eval-lab-results/2026-10-14/repeat-oracle-fix/`.
