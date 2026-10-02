---
status: living
audience:
  - analyst
  - builder
---

# Trial features: one row per trial

`evallab features <job_dir_or_runs_root>... --out <stem>` scans Harbor trial
directories (same discovery rule as `evallab watch`: a path is either a job
dir whose children are `<task>__<id>` trials, or a runs root whose children
are job dirs) and writes `<stem>.parquet` plus a `<stem>.csv` sibling for
humans. The module (`evallab.interpretation.features`) implements no new
detection logic; every signal comes from an existing deterministic detector
(see trust notes).

```bash
uv run evallab features runs/ovn-g5-000169-tuned --out state/g5
```

## Column dictionary

| column | source | meaning |
| --- | --- | --- |
| `trial`, `job` | dir names | trial / job directory names |
| `task_id` | `result.json:task_name` | last `/`-segment (QUIRKS Q23; never the `task_id` dict) |
| `arm` | job/trial name | first `-`-segment in `KNOWN_ARMS` (`gepa/stock/tuned/direct/wrapper/…`); best effort, may be null |
| `n_steps`, `n_agent_calls` | trajectory | all steps / agent-source steps |
| `wall_time_s` | `result.json` | `agent_execution` finish−start, else trial finish−start |
| `input_tokens`, `output_tokens` | step metrics | sums over agent steps (`live_watch`) |
| `mean_input_per_call`, `mean_output_per_call` | derived | totals / `n_agent_calls` (null when 0) |
| `tools_used` | tool calls | sorted unique verbs: bash verbs via `outside_fetch` quote-aware `_segments`/`_program` (lowercased); non-bash tools as `@name` |
| `tool_counts_json` | derived | verb → count JSON |
| `first_edit_step`, `first_edit_paths` | `live_watch.first_repo_edit` | first repo edit via the shared `token_flow` detectors (quoted reads excluded) |
| `n_test_runs`, `test_runs_passed/failed`, `last_test_step` | observations | agent-side pytest only: a step counts when its command invokes `pytest` or its observation has a pytest `===` summary bar; counts from `N passed/failed` tokens (nulls when no runs) |
| `n_errors`, `n_exceptions` | observations | steps matching tool-error markers / containing a `Traceback` marker (may overlap) |
| `loop_onset_step` | `token_flow.analyze_token_flow` | earliest repetition-without-progress onset (null when none) |
| `max_identical_run` | `live_watch` | longest trailing-identical-command run |
| `n_fetch_attempts`, `first_fetch_step` | `upstream_fetch` via `live_watch` | every classified attempt counts, failed or not; first step id |
| `n_fetch_confirmed` | same | attempts with confirmed acquisition evidence |
| `copy_verdict` | `counts.classify_counts` | `counted_pass/counted_fail/excluded`; taint is the `copy_check` flag only |
| `stop_reason` | `step_layers.classify_stop_reason` | `trial_budget_exhausted/agent_timeout/error/task_complete/…`; final-claim flag comes from the last agent step (prose-completion mapping needs step layers, so those stay `unknown`) |
| `limit_hit` | `probe03.ceiling_which` | binding ceiling for budget stops (`input_tokens/…`), else `agent_timeout`/`loop_break`/null |
| `reward` | `verifier_result.rewards.reward` | null when unscored |
| `final_diff_lines`, `files_touched`, `diff_source` | `verifier/agent.diff`, else edit steps | `+/−` line count + `+++` files when the harness diff exists (`verifier/agent.diff`); otherwise the union of edit-step touched paths with null line count (`edit_steps`) |

## Trust notes

- Read-only: never touches `runner.py`, the proxy, or trial files.
- One bad trial degrades to a null row, never kills the table.
- `copy_verdict` sees copy taint only, not fetch taint: a passed trial
  with a confirmed upstream fetch but no `copied_code` flag still reads
  `counted_pass`. Fetch evidence is in `n_fetch_confirmed` instead.
- Test/error columns cover **agent-side** runs only; the harness verifier
  (`verifier/test-stdout.txt`, `ctrf.json`) is out of scope.
- `arm` is a naming-convention parse, not ground truth.
- Validation: `tests/test_features.py` (quoted-awk read is not an edit;
  failed fetch is an attempt; ceiling mapping) plus a 3-trial hand
  spot-check recorded in the HAR-159 receipt.
