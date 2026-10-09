# MiMo clean-set verification (fleet census)

Date: 2026-10-09. Owner: FleetCensus lane. Runner: `src/evallab/mimo_census.py`,
CLI: `evallab mimo-census run|report|record-spend`. Receipt:
`research/experiments/mimo-clean-census/` (`results.csv` + `README.md`).

## What the census proves

For every clean-set manifest row, four checks on the FINAL clean package
(digest-bound, not the historical HAR-191 rows):

(a) **nop = 0 with tests executed** — the nop control grades 0 AND the
    trial's `verifier/junit-grade.log` shows named test cases ran
    (`cases>0 and named>0`). A 0 without test evidence grades `0-noexec`
    (needs triage), never pass.
(b) **oracle = 1 where a reference fix exists** — manifest `reference_fix`
    points at the HAR-191 solution patch; the oracle control must grade 1.
    Rows with `reference_fix = none` report `n/a`.
(c) **cheat ladder = clean** — all 12 attacks of `evallab.cheat_ladder.ATTACKS`
    (v1.2.0, executed through the model-free `cheat` agent; payloads reused,
    never forked) run with status `executed`, and the benchmark's own verifier
    reward grades every trial 0. A cracked full-ladder trial triggers
    per-attack single trials for attribution; unattributed cracks report
    `full-ladder-unattributed`.
(d) **fix-content census = 0 locations** — for tasks with a recoverable fix,
    `evallab.fix_content_census` probes the published image (pre-cleanup run
    package, fix recovered from image history) and then the clean chain
    composed with the real transform functions. The clean-chain open-leak
    count must be 0.

## Backends and parity

- `docker` — locked local Docker (`--network none` from creation), $0.
  Parity baseline: the same Harbor trial path as
  `evallab mimo-clean verify-local` under a census job namespace.
- `modal` — Harbor 0.24's native ModalEnvironment running the REAL clean
  package semantics: embedded-healthcheck setup, agent phase, workspace
  snapshot, then the bundled separate-verifier grading in a FRESH sandbox
  from the same pinned image. No HAR-191 shortcuts (oracle/nop sharing one
  sandbox); the fresh-verifier boundary is Harbor's own separate-verifier
  lifecycle.

Parity gate (required before any paid batch): on ≥5 tasks, local-Docker
Harbor results == census-runner docker-backend results for oracle/nop/ladder.
Modal batches additionally compare per-attack ladder detail (especially
`upstream_fetch`) against Docker to prove the network block holds on Modal.

CLI surface note: the census ships as top-level `evallab mimo-census`
(deliberate — CleanSetV2 declined an in-file `mimo-clean census` hook so both
lanes merge independently; see lane handoff 2026-10-09).

## results.csv contract

Columns: `task_id, manifest_version, final_digest, nop, oracle,
ladder_verdict, ladder_cracking_attacks, census_locations, backend, cost_usd,
run_ids`. `cost_usd` is the task's amortized share of its batch's PROVIDER
actuals (recorded via `record-spend`); `run_ids` are census job names under
the out-of-git jobs dir. Assemble with `evallab mimo-census report`.

## Manifest `verify` grades (census-owned column)

`verified-clean` (all checks pass) · `open-leak` (census_locations > 0) ·
`grader-hole` (ladder cracked) · `env-broken` (nop fails: setup/grade broken
with evidence) · `oracle-wrong` (oracle != 1 with fix, controls otherwise
clean) · `infra-flake` (transient infra failure, passes on retry) ·
`needs-triage` (ambiguous signals: `0-noexec`, partial ladder, unattributed
crack) · `unverified` (missing/unscored cells). Mapping:
`mimo_census.verify_grade_for`; row acceptance: `mimo_census.census_row_pass`.

## Spend discipline ($15 slice cap)

`run --backend modal` refuses when slice actuals (receipt `spend.jsonl`) +
`n_tasks × --worst-case-usd-per-task` exceeds the cap. Per-batch provider
actuals (Modal billing deltas, not estimates) are recorded with
`record-spend --evidence <receipt>` before the next batch launches.

## Results

Tracked in the receipt README (`research/experiments/mimo-clean-census/`),
not here: pass rates, every failure classified (env broken / oracle wrong /
real leak / real grader hole / infra flake) with evidence, spend actuals,
and any real leak or grader hole written up as an open issue.
