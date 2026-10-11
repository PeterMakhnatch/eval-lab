# mimo-clean-v4 census receipt (fleet regrade under separate-verifier@6)

Date: 2026-10-10/11. Owner: V4Regrade (sole owner of the v4 manifest
`verify` column and of this receipt). Slice approval: **$6.50 fresh
provider actuals** (from Main; within Peter's $25 programme approval).
Method: `docs/mimo/verification.md` (+ v4 carry rules below). Runner:
`evallab mimo-census run|report|record-spend` (Modal LIMIT grading cells;
local Docker controls). Config: `--timeout-seconds 900` bulk,
`--modal-resource-policy limit`, 24-wide remote fan-out (48-wide throttles
per the v3 receipt), Modal billing app `mimo-clean-census`
(`ap-8b0CQZyeIkELvgpjYWb3rH`).

## Scope and package identity

Target: `mimo-clean-v4` (chain
`strip-future-history@1>purge-build-caches@4>mtime-normalize@2>separate-verifier@6>agent-network-none@1`;
2,666 built; 564 reference fixes: 256 carried + 308 new). Manifest
`research/experiments/mimo-clean-v4/manifest.csv` (sha256 at census start
`9719729c…`; `verify` filled by this slice, see below).
`results.csv` is the v4 fleet result (399 rows over 391 tasks + 4
Docker-backend rows; exact-digest-bound, never merged across
generations). Per-task evidence: `verify-detail.csv` (all 2,698 rows:
grade + fresh cells + carries). Samples/quotas/digests: `sampling.json`
(seed `20261010`; ladder-100 and step-4 samples disjoint by design).
Raw evidence: `~/Developer/eval-lab-results/2026-10-10/mimo-clean-v4-census/`
(`census-rows.jsonl`; exact-digest rows only).
No fix-content (check d) cells were run in this slice: the probe adapter
has known FP/FN mechanisms and ProbeV2 owns fixes + re-triage of the 71
v3 signature-hit tasks. The 25 v3 `fail:open-leak` labels were NOT copied
into v4 (all 25 verified still `unverified:*`, none `fail:open-leak`).

## Step 1 — pilot (002552, 000163, 000208-Go; all checks)

| backend | 002552 | 000163 | 000208 (go) |
|---|---|---|---|
| docker | oracle 1, nop 0, ladder clean | same | same |
| Modal LIMIT | oracle 1, nop 0, ladder clean | same | same |

Parity gate passes on @6 packages (Docker == Modal LIMIT, identical
digests). Pilot issue found and fixed: `cheat_ladder.CHEAT_AGENT_VERSION`
still read `1.3.0` while `ATTACKS` already carried the 14-attack 1.4.0
content (#829 added the attacks but never bumped the stamp; the native
agent stamped `attempts.json` 1.3.0). One-line bump to `1.4.0`
(`src/evallab/cheat_ladder.py`); pilot ladders re-run on Docker for clean
1.4.0 stamps (content was always 14 attacks — all pilot trials execute
all 14; Go skips Python-only source attacks legitimately).
`tests/test_mimo_census.py` + `tests/test_cheat.py` green after the bump.

## Step 2 — @6 regression (000163, 000200, 000203; were fail:0 under @4/@5)

| task | nop | oracle | note |
|---|---|---|---|
| 000163 | 0 | 1 | via pilot cells, no duplicate spend |
| 000200 | 0 | 1 | Modal LIMIT |
| 000203 | 0 | 1 | Modal LIMIT |

The @4 skip-counted-as-bad and multi-phase-clobber shapes grade correctly
under @6. No v4 rebuild needed for these three.

## Step 3 — ladder 1.4.0 on 100 random built tasks (60 py / 40 non-py)

95 `clean`, 0 `cracked`, 2 `unscored`, 3 timeouts (no row). One retry of
the 9 incomplete: +4 clean; the 5 rest persistent (below). **Any-crack
rule: no crack observed, no stop triggered.**

| outcome | tasks |
|---|---|
| clean (95) | incl. all 14 attacks executed on py tasks; Go skips py-only source attacks |
| unscored | 001421 (HealthcheckError x2), 002607 (RewardFileNotFoundError x2) |
| timeout, no row (900 s cap, 1500 s aggregate fail-safe) | 000245, 000306, 001204 |

Non-py ladder mix: go 19, js 8, ts 7, unknown 2, ruby 2, c++ 1, php 1.

## Step 4 — oracle+nop on 120 new-fix tasks (50 py / 30 js / 20 ts / 20 go)

119/120 banked (001576 triple-timeout, no row). One tool-timeout kill
mid-batch; remainder re-run in two chunks.

| (nop, oracle) | count |
|---|---|
| (0, 1) | 71 |
| (0-noexec, 1) / (0-noexec, 1-noexec) | 14 / 18 |
| (0 / 0-noexec, fail:0) | 7 / 7 (all 14 triaged, none oracle-wrong) |
| setup-fail involved | 2 rows (000276: nop setup-flake, oracle 1; 000928: double setup-fail) |

## Step 5 — oracle on more new-fix tasks (remainder of cap)

All 172 remaining new-fix tasks without oracle cells + top-up of 15
ladder-sample new-fix tasks + setup-fail retry (9 tasks nop+oracle) +
$0 Docker cross-checks (000081; 4 TS setup-fails). Final new-fix oracle
coverage: **307/308** (only 001576 without, triple-timeout).
B4: 163/172 banked (101×1, 42×1-noexec, 14×fail:0 triaged, 6×setup-fail);
retry: +6 banked, 3 more timeouts (000583/001691/001763, double-timeout).
Top-up 15/15 banked. Setup-fail retry: 4 transient flakes recovered
(000096/000276/000656/001514 now oracle 1 + nop 0), 5 persistent.

## The 30 oracle fail:0 — all triaged, zero fail:oracle-wrong

Every fail:0 was root-caused to trial evidence. 29 are @6
grader-certification gaps (tests visibly pass, rc 0 — the grader cannot
certify the shape); 1 is a setup failure; 1 is pre-existing failures.
**No indexed reference fix was found genuinely wrong.** Labels stay
`unverified:<cause>`; nothing was marked `fail:oracle-wrong`.

| id | shape | tasks |
|---|---|---|
| G1 | go `[no tests to run]` substring overfire: multi-package `go test ./...` with a test-less sub-package (real `--- PASS` + `ok`) grades 0 | 000048 |
| G2 | jest `Tests: N skipped, M passed` shape: PASS regex needs digits right after `Tests:` | 000133, 000867, 001270, 001403, 001410, 001411, 001916, 000632 |
| G3 | go direct-binary `PASS` without `ok` line (`go test -c` + exec binary) | 000248 (nop `--- FAIL`, oracle `PASS` — fix works) |
| G4 | vitest ANSI color escapes split marker text | 000828, 001548 |
| G5 | pytest pristine collection-error vs fail-to-pass key mismatch (fix resolves the import, baseline error unmappable) | 000083, 000201, 000700, 000783, 001220, 001431, 002419, 002508, 002615, 002995, 003023 |
| G6 | tap-labeled command emitting mocha spec-reporter output (`19 passing`, no TAP `ok`) | 000934, 000936, 001839 |
| G7 | multi-phase `set -e` baseline truncation (pristine phase-1 failure fixed by the agent run, comparison blind) | 000511 |
| G8 | baseline unreliable (pytest-flaky's own suite; INTERNALERROR on pristine) | 000585 |
| ENV | setup breaks pre-test (000703: vintage setup.py `dist_info` vs modern setuptools, x2 deterministic) | 000703 → env-broken |
| PRE | fix-independent failures identical with/without fix, reproduced on Docker (000081 xdoctest format assertions) | 000081; sweep's `oracle:pass` label needs re-examination by the reference-fix lane |

G1–G8 are filed here as grader-lane follow-ups (separate_verifier, not
the census runner): the @6 non-empty/marker/baseline rules overfire on
these legitimate shapes. No grader code was changed by this slice beyond
the ladder version stamp.

## Manifest verify (2698 rows; v3 fail:open-leak never copied)

| verify | count | basis |
|---|---|---|
| pass | 6 | non-ref, carried @4 nop=0 + fresh 1.4.0 ladder clean (000022/000087/000223/000472/000745/001001) |
| env-broken | 5 | persistent setup-fail: 000703 (pip metadata, deterministic), 000928/001419/001420/001672 (TS HealthcheckError x2 + Docker cross-fail) |
| unverified:probe-pending | 196 | ref, @6 nop+oracle graded (fresh or carried); fix-content probe outstanding (ProbeV2) |
| unverified:ladder-1.4.0-incomplete | 154 | v3 12/14-attacks clean carried; 2 new source-skip attacks ungraded |
| unverified:nop-missing | 148 | mostly oracle-only ref cells |
| unverified:ladder-missing | 1909 | never ladder-graded (bulk of fleet) |
| unverified:cells-missing | 166 | no cells at all |
| unverified:noexec | 77 | rewards without execution proof (non-Python over-represented: parser narrowness, known v3 limitation) |
| unverified:G1–G8/PRE | 30 | triaged fail:0 table above |
| unverified:timeout | 4 | 000583/001576/001691/001763 (2–3 strikes each at 900/1500 s) |
| unverified:ladder-healthcheck / ladder-no-reward | 1 / 1 | 001421 / 002607 |
| unverified:oracle-missing | 2 | 000818/001249 (carried nop, no oracle cell) |
| fail:* | 0 | zero cracked ladders, zero confirmed wrong fixes, zero copied leak labels |

Carry-forward rules (applied ONLY where documented safe): v3 `nop=0`
implies @6 nop=0 (nop trials run the pristine tree, so @6's added
baseline comparison is trivially satisfied; setup/command frozen);
v3 `oracle=1` implies @6 oracle=1 (@4 counted every skip bad, so 1 means
zero skips/failures — @6's baseline clauses are then vacuous); v3
1.3.0-clean carries 12/14 attacks only. All carries assume deterministic
suites (stated residual risk). Cross-version evidence never merges by
digest: carries are reasoning-backed labels in `verify-detail.csv`, not
`results.csv` rows.

## Finance (provider actuals, Modal billing report, fenced per batch)

Cap $6.50. Spent **$3.5387** (booked == provider cumulative delta on app
`mimo-clean-census`: 10.0983 less 6.5596 baseline; headroom $2.96).
Per-batch actuals in `spend.jsonl` (B3/B4 split pro-rata by selected
cells over their combined bucket delta, labeled; tail true-up reconciles
to the provider cumulative exactly; open-hour buckets may still creep).

| batch | cells | actual USD |
|---|---|---|
| v4-pilot-limit-3 | 3×(nop+oracle+ladder) | 0.0175 |
| v4-regress-limit-2 | 2×(nop+oracle) | 0.0081 |
| v4-ladder100-limit | 100 ladder | 0.7248 |
| v4-ladder100-retry-9 | 9 ladder (4 clean, 3 timeout, 2 err) | 0.1688 |
| v4-step4-batch1/chunk1/chunk2 | 120×(nop+oracle) | 1.0440 / 0.1914 / 0.1914 |
| v4-step5-172 | 172 oracle | 0.7482 |
| v4-step5-retry-9 / remainder | 9 oracle retry | 0.06 + 0.2058 |
| v4-step5-topup-15 | 15 oracle | 0.07 |
| v4-setupfail-retry-9 | 9×(nop+oracle) | 0.10 |
| v4-step4-001576-retry | 1×(nop+oracle), 3rd timeout | 0.0087 |
| docker pilots/cross-checks | — | 0.00 |

Timeouts billed without evidence remain the dominant cost risk
(1500 s aggregate fail-safe burn); the 900 s trial cap bounds it.

## Residual gaps (honest)

- 1,909 tasks never ladder-graded; 166 with no cells; 148 ref tasks
  nop-missing. Full-fleet grading was never funded by this cap.
- Ref-task `pass` is blocked on ProbeV2 probe verdicts (196
  probe-pending with complete grading cells).
- G1–G8 need grader-lane fixes (new transform + rebuild); affected
  tasks cannot certify oracle=1 until then despite working fixes.
- `report` overwrites the manifest `verify` column with un-triaged
  grades — the triaged fill (`/tmp/fill_verify.py` logic, per-task
  evidence in `verify-detail.csv`) must be re-applied after every
  report run (done: manifest matches the table above).
- Determinism assumption on carries; flaky suites could differ.
