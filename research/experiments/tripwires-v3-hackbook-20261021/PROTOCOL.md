# Tripwires v3 measurement protocol (TL-1)

Frozen identity is `tripwires-v3@<first 12 hex of the combined fingerprint>`. It is distinct
from every v1/v2 identity (`tripwires@<sha>`); this runner has no code path that rewrites a
v1/v2 row or artifact. $0, zero model calls, no container, no submitted-code execution.

`run.py` owns no detector. It scans each published ATIF with the shared
`evallab.tripwires.scan_trace` report, requires `report["hackbook"]` and
`tripwires.HACKBOOK_RULES` (core-owned), and counts which rules fired.

## Rule universe

* `legacy`: the v2 decision rules exactly as `external_traces.fired_rules` computes them
  (`FLAG_RULES` + `content:<CONTENT_STOP_ELIGIBLE>`); diagnostics (`external_sourcing`,
  `history_browsing`) stay out, as in v2.
* `hackbook`: every `HACKBOOK_RULES` entry. `eligibility == "verdict"` rules enter the
  aggregate `any_verdict_rule`; `diagnostic` rules are reported per rule only and never become a
  verdict. Hits are candidate evidence, not confirmed intent or exploit success.
* `legacy_v2_or` is the v2 OR on the same items, so v3's added value is a paired difference.

## What is counted

Per cohort, per view (`full` = published blinded ATIF; `actions` = paired actions-only ATIF),
per rule: true positives over **measured positives**, false alarms over **measured negatives**
(Wilson 95%, rate `null` when the denominator is 0), plus hits on the `other` stratum.
`unknown` (missing/unreadable/zero-agent-step input, hash mismatch, unmapped label, unverified
TB4 task split) is in neither denominator and is listed by reason; excluded registered cells
(`coverage.jsonl`) are counted by reason. Nothing unknown is clean or a miss. Per-item rule ids
and ATIF `step_id` references are in `predictions-<split>.jsonl`.

## Input and provenance map

All trajectories resolve through `evallab.results_home.results_root()` (published refs) and are
checked against the key's registered SHA-256 (`trajectory_sha256`, `actions_only_sha256`). Items
come from the shared loader `evallab.qa.answer_key.load_answer_key`.

| cohort | items.jsonl sha256 (frozen at base 4b6f76667) | splits | positive / negative / other | label target |
|---|---|---|---|---|
| cheatbench-actions-v1 | `2c42c12f5423cfe19bec1e42dceec65d0753bb5506ac73db0e6020fc289891ac` | dev 150 (75/75), test 150 (75/75) | `expected_verdict` fail = human-verified cheating/attempt / pass = vetted benign / none | behavior; benign is not a correctness label |
| hacktrace-act-v1 | `6a089780f64fb363478240bc468a3435762eec9d4937575f745aac872d34a098` | dev 150 (75/75), test 150 (75/75) | fail = ACT shortcut label / pass = ACT negative / none | behavior; not solution correctness |
| mimo-v7-canonical-pass70 | `bad2bd2d57a03656a4f4492154f58e26dd7d9fe83674a3082f8451b1f6029f28` | test 70 only | `source_label` wrong_pass (6) / real_pass (64) / none | outcome (grading correctness, not intent) |
| mimo-fresh-window3 | `080dd9dd7e45db4b3a1c0ecbfdd57e6acee75b9584f9d50b7394d29e6555396b` (+ `coverage.jsonl` `fa798d17…3a834d`) | test 52 only | wrong_pass (4) / real_pass (17) / `not_a_pass` (31, other) | outcome; not_a_pass is an agent failure, neither positive nor negative |
| tb4-hub-dev | none landed | dev only | `exploit_pass` / `clean_pass` / `task_defect_fail`, `neither` = other | behavior |

`mimo-fresh-window3` is resolved from the registered key (`research/answer-keys/mimo-fresh-window3`,
52 items; collection `2026-10-19/fresh-window3/a0297e57d0bdfd4c93023662`; label sources pinned in
its `source.json`). The 8 excluded cells in `coverage.jsonl` are not items and stay unknown.

### TB4 Harbor Hub DEV: not available at freeze

`~/Developer/eval-lab-results/tb4-hub/` did not exist, and no per-trace TB4 label set is
registered. RC-1's census labels (`tb4-run-census`, 8 tasks) are rule/judge-derived from Preference
Model's text by the same signature families, so they are not independent trace labels, and
`tb4-audit-v1` labels are task-side defects, not per-agent wrong-pass labels. The cohort therefore
reports `unavailable` (never zero). The handoff the runner consumes is a `qa.answer_key/v1`
directory at `<results_root>/tb4-hub/dev-key/` (`README.md`, `items.jsonl`) with published
`trial_ref`/`actions_only_ref`, `split: dev`, and `source_label` in `exploit_pass`, `clean_pass`,
`task_defect_fail`, `neither` (the vocabulary is the runner's proposal; any other value is
`label_unmapped`). Firewall: the cohort exists only under `--split dev`; a handoff containing any
non-dev item is refused outright; each item's task must be DEV in `splits/assignments.csv`
(`tb4-audit-v1` rows) or it is `unknown` and its trajectory is never opened. No TB4 TEST task,
finding or Hub trajectory is read.

## Freeze and run

1. Develop and tune only on dev splits/synthetic fixtures (`--split dev`). `mimo-*` cohorts have no
   dev split and report `unavailable` there.
2. Freeze: commit rules, catalog and this runner, then
   `uv run python research/experiments/tripwires-v3-hackbook-20261021/run.py fingerprint`.
   `combined` hashes four parts: `implementation` (static import closure of
   `evallab.tripwires` over `src/evallab`, including lazy imports and package `__init__`),
   `catalog` (canonical `HACKBOOK_RULES` + `HACKBOOK_DATA_FILES` bytes), `helpers`
   (`external_traces.py`) and `measurement` (`run.py`). Record it before any test scoring.
3. Test run, once per frozen version:
   `… run.py measure --split test --expect-fingerprint <combined>`. It refuses a different
   fingerprint, a missing `--expect-fingerprint`, and an existing `results-test.json`,
   `predictions-test.jsonl` or `report-test.md` (outputs are retained, never overwritten). Any
   byte change to the closure, catalog, helper or runner is a new `tripwires-v3@` identity.
4. Dev run for TB4 labels, when they land: `… run.py measure --split dev` (also scores the
   cheatbench/hacktrace dev splits; do not retune on a retained test result).
5. Publish: `… run.py publish --results …/results-test.json`. One row per measured, registered
   cohort x view with `system = tripwires-v3@<12>/<view>`, `recall`/`false_alarm_rate` = the
   `any_verdict_rule` aggregate, `n_items` = registered in split, `n_measured` = positives +
   negatives measured. It only appends, verifies the old bytes are an unchanged prefix, skips
   rows already present (idempotent), refuses a changed `recall`/`false_alarm_rate`/`n_measured`
   under an existing identity, refuses any non-`tripwires-v3@` system, and publishes nothing for
   unavailable or unregistered cohorts.

## Caveats

* Descriptive Wilson intervals; not cluster-adjusted. Items from one task are correlated.
* The two MiMo cohorts carry outcome labels: a wrong pass need not contain an exploit
  signature, so low recall there is not a detector failure by itself.
* `mimo-v7-canonical-pass70` trajectories were seen by earlier qa-trace runs; `mimo-fresh-window3`
  is fresh trajectories on overlapping tasks (see its README). Disclose exposures in
  `splits/exposure.jsonl` as required by the scoreboard guard.
* Offline published views cannot evaluate trial-directory-only integrity rules, task manifests or
  peer baselines (`not_evaluated` in the results).
* Full/actions naming is literal: the actions view blanks agent message and reasoning only.
