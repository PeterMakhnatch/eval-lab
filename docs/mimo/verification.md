# MiMo clean-set verification (fleet census)

Date: 2026-10-09. Owner: FleetCensus lane. Runner: `src/evallab/mimo_census.py`,
CLI: `evallab mimo-census run|report|record-spend`. Receipt:
`research/experiments/mimo-clean-census/` (`results.csv` + `README.md`).

## What the census proves

For every clean-set manifest row, four checks on the FINAL clean package
(digest-bound, not the historical HAR-191 rows):

(a) **nop = 0 with tests executed** — the nop control grades 0 AND the
    trial carries test-execution evidence: JUnit grade logs with `cases > 0`
    (`named` is diagnostic only — some suites report cases without parsable
    names), or pytest result/summary lines in `verifier/test_output.log` for
    exit-code-graded suites that emit no JUnit XML. A 0 without evidence
    grades `0-noexec` (needs triage), never pass.
(b) **oracle = 1 where a reference fix exists** — manifest `reference_fix`
    points at the HAR-191 solution patch; the oracle control must grade 1.
    Rows with `reference_fix = none` report `n/a`.
(c) **cheat ladder = clean** — every attack of `evallab.cheat_ladder.ATTACKS`
    (v1.2.0, executed through the model-free `cheat` agent; payloads reused,
    never forked) ends `executed` or legitimately `skipped` (per-attack
    inapplicability with a recorded reason — e.g. source-tamper attacks on
    tasks with no imported package `__init__`), and the benchmark's own
    verifier reward grades every trial 0. Missing/failed attacks grade
    `partial` (unverified). A cracked full-ladder trial triggers per-attack
    single trials for attribution; unattributed cracks report
    `full-ladder-unattributed`.
(d) **fix-content census = 0 locations** — for tasks with a recoverable fix,
    `evallab.fix_content_census` probes the published image (pre-cleanup run
    package, fix recovered from image history) and then the ACTUAL clean
    package setup (what ships — never a recomposed approximation, so new
    transform versions like mtime-normalize@2 are measured exactly). The
    clean-chain open-leak count must be 0.

## Backends and parity

- `docker` — locked local Docker (`--network none` from creation), $0.
  Parity baseline: the same Harbor trial path as
  `evallab mimo-clean verify-local` under a census job namespace.
- `modal` — Harbor 0.24's native ModalEnvironment running the REAL clean
  package semantics: embedded-healthcheck setup, agent phase, workspace
  snapshot, then the bundled separate-verifier grading in a FRESH sandbox
  from the same pinned image. No HAR-191 shortcuts (oracle/nop sharing one
  sandbox); the fresh-verifier boundary is Harbor's own separate-verifier
  lifecycle. Requires the Modal SDK at run time (not in the lockfile):
  `uv run --with "modal>=1.5.4" --with "dockerfile-parse>=2.0.1" --extra
  laminar evallab ...` plus `~/.modal.toml` auth.
  **Known block (2026-10-09, open issue)**: mtime-normalize@1's fail-closed
  check trips on Modal `_ModalDirect` (`Image.from_registry` lazily
  materializes layers, re-bumping directory mtimes after the touch walk;
  repeat touch+check in the same sandbox shows 0 offenders). Setup fails
  with `mtime-normalize@1 found paths newer than the fixed timestamp`
  before any agent phase — every Modal cell grades `setup-fail`. Same
  package passes on Docker (eager materialization). Hardening the transform
  (materialization warm pass) is routed to the mtime owner via Main; the
  census does NOT work around it in-runner.
- `daytona` — bounded Daytona sandbox env (`DAYTONA_API_KEY`), the paid
  fallback while Modal-direct is blocked. Controls run under the default
  MiMo egress lock; the cheat agent is outside every lock set and
  Daytona+MiMo additionally refuses explicit unlocked, so cheat cells are
  refused at dispatch and recorded `backend-unsupported` (never failures).
  Cheat coverage for those tasks comes from a backend that admits it as a
  SEPARATE results row: results.csv is keyed (task_id, backend), so one task
  may carry a daytona row (controls) plus a docker row (full ladder).

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
`grader-hole` (ladder cracked) · `env-broken` (nop `fail:` with evidence, or
`setup-fail`: errored jobs such as fail-closed setup blocks — e.g. a
cache-purge block with healthcheck rc=1 and no reward; never bucketed as a
grading failure) · `oracle-wrong` (oracle != 1 with fix, controls otherwise
clean) · `infra-flake` (transient infra failure, passes on retry) ·
`needs-triage` (ambiguous signals: `0-noexec`/`1-noexec`, partial ladder,
unattributed crack) · `unverified` (missing/unscored cells). Mapping:
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
