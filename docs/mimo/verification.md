# MiMo clean-set verification (fleet census)

Date: 2026-10-10. Owner: CensusFinish lane. Runner: `src/evallab/mimo_census.py`,
CLI: `evallab mimo-census run|report|record-spend`. Receipt:
`research/experiments/mimo-clean-census/` (`results.csv` + `README.md`).

## What the census proves

For every clean-set manifest row, four checks on the FINAL clean package
(digest-bound, not the historical HAR-191 rows):

(a) **nop = 0 with tests executed** — the nop control grades 0 AND the
    trial carries recognized test-case evidence: countable JUnit/surefire
    reports, the verifier's explicit runner markers, or runner-appropriate
    result/summary lines in `verifier/test_output.log`. `named` is diagnostic
    only; `cases > 0` is sufficient. An exit code alone never proves case
    execution. `0-noexec` means no recognized execution proof, not a claim
    that a custom harness necessarily ran no tests; it remains unverified.
(b) **oracle = 1 with tests executed where a reference fix exists** —
    manifest `reference_fix` identifies the indexed HAR-191/sweep patch.
    Reward 1 without execution evidence grades `1-noexec`, never pass.
    Rows with `reference_fix = none` report `n/a`; that is not a solvability
    proof.
(c) **cheat ladder = clean** — every attack of `evallab.cheat_ladder.ATTACKS`
    (v1.4.0: 14 attacks including `tamper_source_skip` and
    `tamper_source_skiptest`, executed through the model-free `cheat` agent;
    payloads reused, never forked) ends `executed` or legitimately `skipped`
    (per-attack inapplicability with a recorded reason — e.g. source-tamper
    attacks on tasks with no imported package `__init__`), and the
    benchmark's own verifier reward grades every trial 0. Missing/failed
    attacks grade `partial` (unverified). A cracked full-ladder trial
    triggers per-attack single trials for attribution; unattributed cracks
    report `full-ladder-unattributed`. Ladder cells are version-bound: a
    1.3.0-clean carries 12/14 attacks only; the 2 source-skip attacks need
    fresh 1.4.0 trials.
(d) **fix-content census = 0 locations with a real positive control** —
    for all reference tasks, `evallab.fix_content_census` stages signatures
    from the actual indexed patch and measures BOTH the published-image
    baseline setup and the ACTUAL shipped clean-package setup. No synthetic
    marker is injected, and no setup is recomposed. Published zero is
    `probe-blind`, even if the clean scan also finds zero; it cannot prove
    cleanliness. Setup, readiness, probe, and scan completion must all be
    evidenced before a clean location count can pass. Both outcomes and
    sandbox lifecycle are retained, including incomplete/blind controls.

## Backends and parity

- `docker` — local Docker, $0, honoring the package's declared network
  contract, never a census-only runtime egress lock. Historical v2 packages
  declare `network_mode = "public"`, so `upstream_fetch` had real agent
  egress. Canonical v3 adds `agent-network-none@1`: only the agent phase is
  blocked; setup and the separate verifier remain public. The ladder now
  measures that shipped restriction, not our backend override. Parity uses
  the same Harbor trial path as `evallab mimo-clean verify-local` under a
  census job namespace.
- `modal` — Harbor 0.24's native ModalEnvironment running the REAL clean
  package semantics: embedded-healthcheck setup, agent phase, workspace
  snapshot, then the bundled separate-verifier grading in a FRESH sandbox
  from the same pinned image. No HAR-191 shortcuts (oracle/nop sharing one
  sandbox); the fresh-verifier boundary is Harbor's own separate-verifier
  lifecycle. Requires the Modal SDK at run time (not in the lockfile):
  `uv run --with "modal>=1.5.4" --with "dockerfile-parse>=2.0.1" --extra
  laminar evallab ...` plus `~/.modal.toml` auth.
  Historical v2 parity hit two now-fixed setup portability faults:
  mtime-normalize@1 (fixed by @2, PR #813) and purge-build-caches@3
  (fixed by @4, PR #823). In pip 25.3 any valid nonempty
  `PIP_NO_CACHE_DIR` value disables caching, including Modal's `"off"`;
  @3 misclassified that disabled state as a purge failure. The 3/3
  failed v2 parity cells remain historical `setup-fail` evidence.
  @4 has a full Modal final-setup proof (`rc=0`, `ready=yes`);
  final-v3 native reward/resource-policy parity is recorded separately below.
- `daytona` — bounded Daytona sandbox env (`DAYTONA_API_KEY`), an alternate
  control backend. Controls run under the default
  MiMo egress lock; the cheat agent is outside every lock set and
  Daytona+MiMo additionally refuses explicit unlocked, so cheat cells are
  refused at dispatch and recorded `backend-unsupported` (never failures).
  Exact mechanism (`src/evallab/execution_contracts.py`): MiMo+Daytona
  mandates `egress_lock=true` (explicit `false` refused, no silent unlocked
  fallback) and `EGRESS_LOCK_AGENTS["daytona"]` excludes `cheat`, because
  the ladder's network attacks (notably `upstream_fetch`: `git ls-remote`,
  `cheat_ladder.py`) need sandbox egress a locked sandbox cannot provide.
  Assessed 2026-10-10, no change: an unlocked Daytona MiMo run needs a
  policy change (out of lane), and admitting `cheat` to the lock set would
  weaken V6 (untestable under lock) while Docker ladders test it with
  egress. Ladders stay on Docker/Modal.
  Cheat coverage for those tasks comes from a backend that admits it as a
  SEPARATE results row: keys include task, backend, manifest version, exact
  digest, and Modal policy; historical generations are never relabeled.

Parity gate (required before any paid batch): on ≥5 tasks, local-Docker
Harbor results == census-runner docker-backend results for oracle/nop/ladder.
Modal batches additionally compare per-attack ladder detail (especially
`upstream_fetch`) against Docker with the same package network contract.
Controls and ladders retain their exact package digest/version; changing
the shipped agent network policy does not silently relabel earlier rows.

Modal census trials use the billing app `mimo-clean-census` and known-patch
fix probes use `mimo-clean-census-fix`, separating this slice from other
lanes' shared `__harbor__` usage. `RunRequest.modal_app_name` only forwards
Harbor's `app_name` environment argument; it does not grant paid approval
or modify the package's network policy.

`--modal-resource-policy auto` is the unchanged Harbor default. The
census-only opt-in `limit` forwards `cpu_enforcement_policy=limit` and
`memory_enforcement_policy=limit`: the package's CPU and memory **caps stay
unchanged**, while Modal may bill a smaller idle request rather than the
full cap. The override is rejected outside Modal and outside the census
billing app. Each result records its policy; reusable cells must match it.
Before fleet use, compare AUTO and LIMIT on at least three tasks against
the Docker rewards, including the ladder, and check for OOMs/timeouts.

The final-v3 gate passed on 000085, 000158, and 002552: Docker, Modal AUTO,
and Modal LIMIT all returned oracle 1, nop 0, and a clean full ladder on
identical digests. All 18 controls have actual test-execution evidence; all
27 trials have no reported OOM, timeout, or trial exception. The exact
configs, caps, raw paths, and grades are in
`research/experiments/mimo-clean-census/modal-limit-parity.json`.
The direct SDK fix probes are separate AUTO/default-resource executions,
not native LIMIT trials; their lifecycle metadata records the actual SDK
path and the isolated fix-probe app.

CLI surface note: the census ships as top-level `evallab mimo-census`
(deliberate — CleanSetV2 declined an in-file `mimo-clean census` hook so both
lanes merge independently; see lane handoff 2026-10-09).

## results.csv contract

Columns: `task_id, manifest_version, final_digest, nop, oracle,
ladder_verdict, ladder_cracking_attacks, census_locations, backend, cost_usd,
run_ids, ladder_version, modal_resource_policy`. `cost_usd` is an amortized
batch allocation, not a per-task invoice; inherited Daytona allocations are
rate-card reconstructions as explained below. `run_ids` are census job names
under the out-of-git jobs dir. Assemble with `evallab mimo-census report`.

## Manifest `verify` grades (census-owned column)

`pass` (all applicable checks complete and pass) · `fail:open-leak`
(clean-chain fix content survives) · `fail:grader-hole` (ladder cracked) ·
`fail:oracle-wrong` (reference fix fails the grader) · `env-broken`
(evidenced setup/test-environment failure) · `unverified` (missing cells,
backend refusals, partial/unscored ladders, no test-execution evidence, or
probe-blind known-patch census). Per-check columns retain finer failure
classes even when a task cannot receive an overall pass. A published-image
probe with no positive hit is **probe-blind**, not evidence of cleanliness.
Mapping: `mimo_census.verify_grade_for`; row acceptance:
`mimo_census.census_row_pass`. Evidence is bound to the exact `final_digest`;
cross-backend controls and ladder/fix cells may combine only for that digest.
An older version's outcome never silently grades a newer package.
Cross-generation carries are allowed ONLY where documented safe, with the
reasoning recorded in the grading receipt (v4 rules in
`research/experiments/mimo-clean-v4-census/README.md`): a prior nop `0`
implies the current nop (nop trials run the pristine tree, so an added
baseline comparison is trivially satisfied); a prior oracle `1` under a
skip-strict grader implies the current oracle (zero skips/failures makes
fail-to-pass clauses vacuous) — both assuming deterministic suites.
v5 (`@6`→`@7`→`@8`) carries: each generation only ever widens pass paths
the previous one provably misgraded, so no prior-1 output flips to 0.
Concretely: go marker-absent runs reduce to the old rule, and
marker-present runs keep a surviving-`ok` requirement no prior-1 run can
fail (a prior-1 has no marker at all); jest/vitest tolerate strictly more
summaries while every fail marker is unchanged; tap switches reporters
only when no TAP marker exists (every prior-1 tap run has one), and the
terse switch additionally needs terse markers with no TAP/mocha markers
while the terse pseudo-runner sits inside the structured gate (never the
exit-code fallback); pytest fail-to-pass only clears `ftp_bad`
(INTERNALERROR skip, collection-error module satisfaction — `@8` merely
admits dashes in the same prefix-matched comparison, more baseline phases
from every-phase baselines — and a prior-1 run has rc 0 with no bad cases,
i.e. every executed phase passed, so the wider baseline is satisfied too).
Hence a v4 oracle `1` carries to v5 and a v4 nop `0` carries
(failure-shaped outputs still fail every rule); carries stay
reasoning-backed labels in `verify-detail.csv`, never merged `results.csv`
rows.
`fail:open-leak` from untriaged probe hits is NEVER carried: it needs the
probe lane's per-task verdict first. A fresh oracle `fail:0` is never
labeled `fail:oracle-wrong` without per-task root cause reaching the trial
evidence (grader-marker gaps and setup failures are `unverified:<cause>`,
not wrong fixes). Triaged/manual verdicts live in the receipt's
`verify-overrides.csv` (`task_id,verify,reason,final_digest`) and merge
over untriaged aggregates at every `report` run
(`mimo_census.apply_verify_overrides`; `--overrides` to point elsewhere),
so re-running report never loses them; per-task evidence lives in the
receipt's `verify-detail.csv`. An override with a nonempty `final_digest`
applies only on that exact digest; rows without aggregates are never
touched.

## Fix-content probe v2 (256 reference tasks + 196 v4 probe-pending)

Probe v2 (`src/evallab/fix_content_census.py`, `batch` subcommand; receipt
`research/experiments/fix-probe-v2/README.md`) closes every gap hand-triage
found in the v1 check (d) (65 clean signature hits, 164 probe-blind):
post-fix blob SHA + whitespace-normalized variant per changed non-test file
(derived container-side via `git apply` of the known patch; searched in
same-basename carriers and `.git` objects, so deletion-only 001955 and
18-char 002378 are covered); per-pattern base subtraction (no shadowing);
imports/short lines excluded; generic idioms (one pattern in ≥ 3 unrelated
dirs) dropped; harness paths (`/__modal`, `/proc`, `/sys`) and the probe's
own scratch excluded; a location counts with ≥ 2 co-located fix-specific
lines or a blob hash; published fast control (fix commit or post-fix blob
pre-setup counts, else `blind` with reason).

Fleet result, 256/256 reference tasks probed published + clean ($0 local
Docker): published probe-blind **164 → 0**; clean real-hit tasks 22 of 256
(v1: 65 signature-hit tasks). Hand triage of all 22: **11 REAL E1 leaks**
(000255, 000322, 000466, 000497, 000545, 000812, 002371, 002384, 002668,
002790, 002829 — byte-identical post-fix project files in site-packages /
`build/lib` / baked venv / stray checkout; every chain skipped
purge-installed-copies@1 under the HAR-194 scope stance; worktree at base in
all 11; proof in `~/Developer/eval-lab-results/2026-10-11/fix-probe-v2/real-leaks.md`),
5 answer-visible via env tooling/dependencies (001478 geopandas, 001671
escapism, 001945 scipy, 001971 setuptools vendor, 002414 pypoetry venv —
reference implementation readable, not purge-removable; 002414 confirmed
@2-by-design by PurgeV2), 6 probe coincidences (000636, 000926, 001433,
002549, 002786, 003057 — stdlib idioms, upstream text, README/METADATA hints
without implementation). 1 clean blind (002389, clean setup rc=1
deterministic: purge-build-caches@2 precondition); 4 tasks have no clean
package (published-only rows). Follow-up purge-installed-copies@2 (PR #836)
closes all 11 E1 on the final chain (PurgeV2 verified); this slice changes
no transforms.

Second run, 109/109 new-index tasks against v4 packages (same probe):
8 blind (`no-fix-signal`, patternless with no fix objects — stay pending),
8 real-hit tasks triaged to **1 REAL** (000919, baked node `lib/` build
output with fix content), 1 V6 residual (001690, newer env keras), 6
coincidences. v3/v4 setup bytes are identical for the 83 overlapping tasks,
so v3-package verdicts transfer to v4 rows.

Manifest updates in this slice: v3 `verify` — 20 refuted `fail:open-leak`
→ `pass`, 6 new real leaks → `fail:open-leak` (11 total open leaks);
v4 `verify` — 184 of 196 `unverified:probe-pending` resolved (176 `pass`,
8 `fail:open-leak`: 000255/000322/000466/000497/000545/000812/000919/001690),
12 stay pending (4 no clean package, 8 blind). Per-task verdicts
(chain/digest-bound, for v5): `research/experiments/fix-probe-v2/verdicts.json`.

## Spend discipline ($13 slice cap)

`run --backend modal`/`daytona` refuses when recorded receipt amounts
(`spend.jsonl`) + `n_tasks × --worst-case-usd-per-task` exceeds the cap.
The caller includes unsettled/historical reservations in that projection.
Named Modal app billing gives attributable provider actuals. Inherited
Daytona `cost` output is a rate-card reconstruction, **not an invoice**:
it omits the overlap of the agent sandbox and fresh separate verifier.
`spend-attribution.json` records that limitation, the conservative cap
reservation, and the historical shared-Modal attribution uncertainty.
Never relabel these reconstructions as provider-billed actuals.
Daytona images exceeding the 10 GiB per-sandbox disk grade
`backend-unsupported` (never failures); as many as feasible run on local
Docker at the end (≤2 concurrent trials on the shared daemon).

`run --checks nop,oracle|ladder|fix` permits separate backend phases.
Remote phases use bounded process workers (at most 19); Docker remains at
most two. Whole-batch spend admission happens before workers start, and
only the parent appends results. Partial rows merge per check instead of
erasing previously completed controls. Reusable cells must match the
package digest and, for ladder cells, the attack version.

## Results

Tracked in the receipt READMEs (`research/experiments/mimo-clean-census/`
for v3, `research/experiments/mimo-clean-v4-census/` for the v4 regrade
under separate-verifier@6, `research/experiments/mimo-clean-v5-census/`
for the v5 regrade under separate-verifier@8 + purge-installed-copies@2),
not here: pass rates, every failure classified
(env broken / oracle wrong / real leak / real grader hole / infra flake)
with evidence, spend actuals, and any real leak or grader hole written up
as an open issue. `mimo-clean-v5` is the canonical set
(`docs/mimo/clean-set.md`).

### v5 @8+@2 regrade of the 30 triaged tasks (all fresh Modal LIMIT cells)

Oracle on the final chain: 26x1, 1x1-noexec (000585 G8), 3xfail:0 (000081
PRE, 000703 ENV, 001431 G5-open). Nop on the final chain: 19x0, 16x0-noexec,
zero failures. Per-G oracle:

| G | shape | oracle |
|---|---|---|
| G1 | go nonempty-substring | 1/1 (000048) |
| G2 | jest skipped-shape | 8/8 |
| G3 | go direct-binary | 1/1 (000248) |
| G4 | vitest ANSI | 2/2 (000828, 001548) |
| G5 | pytest collection-error ftp | 10/11 (001431 open, see below) |
| G6 | tap/mocha reporter | 3/3 (incl live terse 000936=1) |
| G7 | multiphase baseline | 1/1 (000511) |
| G8 | baseline unreliable | 1-noexec (000585) |

Certified live under @8: 000783=1 and 001220=1 (dashed-dir fix), 000936=1
(terse fix). 001431 stays `unverified:G5-open`: the fix works (agent rc=0,
138 cases, 0 bad) but the pristine class-level collection error yields a
junit with only the error case, so the G5 exemption misfires — grader-lane
fix prescribed in the v5 receipt, no grader change this slice. 000585 stays
`unverified:baseline-unreliable` (1-noexec). Full per-task evidence:
`research/experiments/mimo-clean-v5-census/verify-detail.csv`.
