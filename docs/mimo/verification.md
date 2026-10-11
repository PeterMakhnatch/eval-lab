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
`fail:open-leak` from untriaged probe hits is NEVER carried: it needs the
probe lane's per-task verdict first. A fresh oracle `fail:0` is never
labeled `fail:oracle-wrong` without per-task root cause reaching the trial
evidence (grader-marker gaps and setup failures are `unverified:<cause>`,
not wrong fixes). `report` overwrites the manifest `verify` column with
un-triaged grades, so the owning slice re-applies its triaged fill after
every report run; per-task evidence lives in the receipt's
`verify-detail.csv`.

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
under separate-verifier@6), not here: pass rates, every failure classified
(env broken / oracle wrong / real leak / real grader hole / infra flake)
with evidence, spend actuals, and any real leak or grader hole written up
as an open issue.
