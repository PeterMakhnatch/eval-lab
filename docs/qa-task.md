---
status: living
audience:
  - analyst
  - operator
---

# QA Agent - Task: bounded defensive task/grader auditor

`evallab qa task` (`src/evallab/qa_task.py`) implements a bounded defensive
task and grader auditor under the shared `qa.finding/v1` and
`qa.answer_key/v1` schemas in `FOCUS.md`. It runs neutral `S0` controls and
read-only static/leak inspections, read-only `S1` complete-rubric review
through the subscription text-only Codex lane, inert `S2` candidate finding
import (`--evidence`), and an inert `S3` numeric triplet gate
(`validate_triplet`). Autonomous bypass/exploit generation, scripted cheat
ladders, mutation probes, red-agent runs, and live exploit replay are outside
this defensive mode and are explicitly recorded as `not_run` (never `pass`).

## Stages

| Stage | What runs in `src/evallab/qa_task.py` | Cost | Output rule |
|---|---|---|---|
| `S0` | Unscored static hack scan (`backend.static`), scored `oracle` × 3 and `nop` × 3 neutral Harbor controls (`oracle_floor = 0.9`, `nop_ceiling = 0.1`: any measured `oracle < 0.9` is `broken`, any measured `nop > 0.1` is `weak`; one failing control is enough), and scored read-only `leak` future-history inspection | $0, local Docker | Completed `oracle`/`nop` threshold failures emit `qa.finding/v1` (`refutation.status: "verified"`, non-null `harbor_trial` `reproducer`). Completed `leak` with `has_future_history == True` appends a `reachable` `Signal` to `report.signals` |
| `S1` | Read-only review of the **complete** rubric (`--rubric <path>` or Harbor's built-in rubric) across $k = 3$ independent text-only Codex samples (`codex/gpt-6-luna`, no tool loop); only complete samples contribute votes; 3-sample majority per criterion (`rubric:<name>`) | Subscription (`cost_usd = 0.0`, 60% 7-day quota gate before every call) | `stage_s1` returns `[]` findings; majority `fail` on any criterion in `DEFECT_CRITERIA` appends a `Signal` (`{id, category, evidence_refs, reviewer}`) to `report.signals` |
| `S2` | Optional inert candidate import (`--evidence <path>`, `import_evidence`, unscored check `imported-evidence`). Live exploit/mutation/ladder probes (`live-probes`) are recorded `status: "not_run"` (`scored: True`) | $0, no execution | Imported `qa.finding/v1` records must match `(task_id, task_digest)` and carry a non-null `reproducer`; their `stage` is set to `"S2"` and `refutation.status` is reset to `"unverified"` (`by: "qa-task/evidence-import"`) |
| `S3` | Optional inert numeric triplet gate (`--triplet <path>`, subject-bound `validate_triplet`, unscored check `supplied-triplet`). Live fresh-container exploit replay (`fresh-replay`) is recorded `status: "not_run"` (`scored: True`) | $0, no execution | Imported findings remain `unverified`; `validate_triplet` checks supplied `wrong_submission`, `oracle`, and `nop` dicts without executing any command, returning `"pass"` iff all three are `"measured"` with finite rewards in `[0, 1]` and `wrong_submission > 0`, `oracle == 1`, `nop == 0` (`"fail"` if measured outside bounds, `"unknown"` otherwise) |

### Findings vs signals

Every `qa.finding/v1` (`Finding`) requires a non-null `reproducer`
(`Reproducer {kind: "harbor_trial" | "command" | "patch", path, expected_reward}`).
Unreproduced suspicions—specifically `S0` `leak` future-history detections and
`S1` majority rubric failures on `DEFECT_CRITERIA`—are recorded on the task
report in `report.signals` (`Signal {id, category, evidence_refs, reviewer}`),
never as synthetic `qa.finding/v1` files without a reproducer.

### Measuredness, resource blockers, and default `unknown` verdict

Every check records `status` in `{"measured", "ran_without_usable_result", "not_run"}`.
Missing, unusable, or not-run checks never pass:

- `report.quality_complete` is `True` iff at least one scored check exists and
  **every** check in `report.checks` (including descriptive/unscored `static`
  and `rubric-sample-1..3` checks) has `status == "measured"`.
- Tasks declaring GPUs (`environment.gpus` / `environment.gpu`) or
  multi-service Compose environments (`environment/docker-compose.yaml` or
  `docker-compose.yml`) are blocked before trial execution: `oracle-1..3`,
  `nop-1..3`, and `leak` are recorded `status: "not_run"` (`scored: True`) with
  the blocker reason.
- If `report.error` is non-null (for example, stage exception or mid-audit
  package digest change), `report.verdict` is forced to `"unknown"`.
- Because the default `--stages S0,S1,S2,S3` includes scored `live-probes`
  (`S2`, `not_run`) and `fresh-replay` (`S3`, `not_run`), `quality_complete` is
  `False` on every default four-stage run. Consequently, `report.verdict` on a
  default run (without `report.error`) is `"fail"` when a verified `eval` fail
  finding (`S0` control failure) is present and `"unknown"` otherwise—never
  `"pass"`.
- Even when `--stages S0,S1` is requested, `report.verdict` is `"pass"` only
  when `report.error` is null, `quality_complete` is `True`, no scored check
  failed, `report.signals` is empty, and no unrefuted non-pass finding stands.

## CLI

Only `evallab qa task` and `evallab qa score` are implemented (there is no
`evallab qa replay` subcommand, `--benchmark-checks-dir`, or `--force` flag).
Supported `evallab qa task` options: `--task`, `--key`, `--split {dev,heldout}`,
`--item`, `--packages-root`, `--run-dir`, `--stages` (default `S0,S1,S2,S3`),
`--trial-timeout` (default `1800`), `--rubric`, `--model` (default
`codex/gpt-6-luna`), `--no-model`, `--evidence`, and `--triplet`:

```bash
# Audit one package with an explicit benchmark rubric (e.g., 35-criterion TOML)
uv run evallab qa task --task <package-dir> \
  --rubric <benchmark>/rubrics/task-implementation.toml

# Audit an answer-key split (packages must match pinned digests)
uv run evallab qa task --key envcheck-tb4-39 --split heldout \
  --rubric <benchmark>/rubrics/task-implementation.toml

# Audit S0 controls + leak only (no model calls) into a fresh run directory
uv run evallab qa task --task <package-dir> --stages S0 --no-model \
  --run-dir runs/.qa-task/manual-s0-check

# Import inert S2 candidate findings and check an inert S3 numeric triplet JSON
uv run evallab qa task --task <package-dir> --stages S2,S3 --no-model \
  --evidence <candidate-finding-or-dir> --triplet <triplet.json>

# Score a run directory against an answer key and append to the scoreboard CSV
uv run evallab qa score --key envcheck-tb4-39 \
  --findings runs/.qa-task/<run-id> \
  --split heldout \
  --scoreboard research/qa-scoreboard/scoreboard.csv
```

The default scoreboard path is `research/qa-scoreboard/scoreboard.csv`, using
the existing shared QA-Trace columns. Each invocation appends one row:
`--level verified` (default) or `--level flagged` for diagnostic triage.
JSON always reports both. Rows identify the defensive/partial status; task
scores put paired discrimination at `n/a`, not zero.

### Run directories and digest binding

- Each `evallab qa task` invocation writes to a **fresh, immutable** directory
  inside the checkout (`runs/.qa-task/<ulid>/` by default, or `--run-dir`). If
  the target directory already exists, `evallab qa task` exits with an error
  rather than overwriting or resuming in-place.
- Output layout: `run.json`, `summary.json`, `tasks/<task_slug>/report.json`
  (`evallab.qa_task.report/v1`), `findings/<sha256(finding.id)>.json`
  (`qa.finding/v1`), and `codex/spend.jsonl` when `S1` runs.
- `subject.task_digest` is computed with `task_directory_digest(package)` at
  the start of `run_task` and re-verified before finalization; if package bytes
  change during the audit, `report.error` is set and all findings are demoted to
  `unverified`. When `--key` is used, `key_packages` verifies every package
  directory against `item.subject.task_digest` before any stage runs.

## Models, rubrics, and quota gate

- **Rubric loading**: `--rubric <path>` loads `[criteria]` from a TOML rubric
  (such as a 35-criterion benchmark `task-implementation.toml`); when omitted,
  `load_rubric(None)` falls back to Harbor's built-in rubric (which may contain
  11 criteria). `S1` always prompts the reviewer to evaluate **every** loaded
  criterion, while `DEFECT_CRITERIA` maps defect-bearing criterion names to
  coarse categories when emitting `report.signals`.
- **Read-only bundle**: `bundle_package` reads `instruction.md`, `task.toml`,
  `README.md`, and files under `environment/`, `tests/`, and `solution/` up to
  180,000 UTF-8 characters total. Symlinks, `__pycache__`, binary files, and
  oversize files are listed in `omitted`; omitted or missing evidence requires
  `"unknown"`, never `"pass"`.
- **Subscription lane and 60% quota gate**: `CodexLane` calls `codex/gpt-6-luna`
  (configurable via `--model`) through fresh text-only `CodexTransport`
  requests with no tool loop. Subscription calls have `cost_usd = 0.0` and log
  `SPEND_TAG = "overnight-2026-10-14"` plus token usage to
  `<run_dir>/codex/spend.jsonl`. Before **every** call (no cached allowance),
  `CodexLane.gate()` runs `omp usage --json --redact -p openai-codex` and checks
  the `7d` window's `usedFraction`; if the reading is unavailable or
  `usedFraction >= 0.60`, `QuotaStop` halts remaining `S1` samples and marks
  them `not_run`.
- `model_tokens` counts usable completed replies only. A transport rejection
  can consume subscription tokens even though its review is unusable; raw
  Codex event files retain those attempts. It is a lower bound, not a bill.
  The development smoke observed a Codex experimental-feature warning emitted
  as an error event; the existing transport rejected those turns rather than
  suppressing the warning or treating their reviews as measured.

## Scoring (`evallab qa score`)

`score_findings` (`evallab.qa_task.score/v1`) filters answer-key items by
`--split` (`dev`, `heldout`, or all when omitted) and binds findings and task
reports by exact `(subject.task_id, subject.task_digest)`. Duplicate finding IDs
or duplicate task reports for the same `(task_id, task_digest)` binding are
rejected:

- **Digest binding**: findings and task reports (including signal-only reports)
  whose `task_id` matches a selected item but whose `task_digest` differs are
  excluded from that item's hits and counted in `digest_mismatches`.
- **Verified vs flagged recall and false-alarm rates**:
  - `verified_recall` (on `expected_verdict == "fail"` positives) and
    `verified_false_alarm_rate` (on `expected_verdict == "pass"` negatives)
    require a `verified_hit`: at least one digest-bound `Finding` with
    `fault == "eval"`, `verdict == "fail"`, and
    `refutation.status == "verified"`.
  - `flagged_recall` and `flagged_false_alarm_rate` require a `flagged_hit`: at
    least one standing unrefuted digest-bound `Finding` (`fault == "eval"`,
    `verdict != "pass"`, `refutation.status != "refuted"`) **or** at least one
    entry in the digest-bound report's `report.signals` (from `S1` rubric
    failures or `S0` leak detection).
- **Category match vs root-cause match**:
  - `category_match_recall` measures whether any verified finding on a positive
    item has `f.category` in `expected_category` / `expected_categories`.
  - `root_cause_alignment_fraction` reports the share of positive items that
    have at least one verified finding with an explicit non-null
    `f.root_cause_id`.
  - `root_cause_match_recall` measures whether any verified finding on a
    positive item has an explicit `f.root_cause_id` matching an `id` in
    `item.root_cause`, and is `null` (`None`) when no verified finding on a
    positive item carries a non-null `root_cause_id`
    (`root_cause_alignment_fraction == 0`). **Coarse category agreement is not a
    root-cause match.**
- **Coverage and split-scoped cost fields**: `measured_fraction` (share of
  selected items whose digest-bound `TaskReport` has
  `quality_complete == True`), `observed_fraction` (share of selected items with
  a digest-bound `TaskReport`), `model_tokens` and `cost_per_item_usd` (summed
  only over the digest-bound reports matching the selected split), and
  `negative_label_strengths` (`weak_negative`, `refuted_claim`).
- **No paired discrimination claim**: task answer keys contain standalone task
  packages rather than paired clean/cheat trial trajectories, so `score_findings`
  does not compute or claim paired discrimination. Never tune on the split you
  report (`research/answer-keys/README.md`).

## Limits

- `S2` live exploit/mutation/ladder probing and `S3` fresh-container exploit
  replay are not implemented in `src/evallab/qa_task.py`; imported `S2`
  candidates remain `unverified`, and default `S0,S1,S2,S3` task reports stay
  `unknown` (with `quality_complete = False`) unless an `S0` control failure
  produces a verified `fail`.
- `S1` text reviews and `S0` leak detections produce `report.signals` (counted
  in `flagged_recall` / `flagged_false_alarm_rate`), never `verified` findings.
- Answer-key scores are bounded by the provenance and label strength of each
  key (`envcheck-tb4-39` and `delphik-open-defect-tb2`); see
  `research/answer-keys/README.md`.
