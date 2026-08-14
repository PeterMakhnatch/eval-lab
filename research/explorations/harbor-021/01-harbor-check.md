# harbor check — task-quality rubric

## What it is

`harbor check <task-dir>` wraps each reviewed task as an ephemeral Harbor job.
An evaluator agent (default `claude-code` / `claude-sonnet-4-6`) reads the task
and writes `check-result.json`. The shipped verifier
(`harbor/analyze/check-task-template/tests/validate.py`) accepts that file only
if it covers every criterion in the rubric with `{outcome, explanation}`.

Default rubric (11 criteria) lives at
`harbor/cli/quality_checker/default-rubric.toml`: instruction/test coverage,
anti-cheat, schema, pins, typos, tests-in-image, hardcoded oracle, filenames.

The lab has never run this. Live `harbor check` is a billable agent job; the
local path below drives the same rubric + verifier without a model.

## Demo

```bash
# from ~/Developer/helab-recon
bash explorations/harbor-021/demos/run-check.sh
```

That prints `harbor check --help`, then runs
`demos/run_check.py` on `tasks/event-summary` via the Harbor 0.21 interpreter.
It loads `load_rubric(None)`, assembles the wrapper with
`assemble_check_task()`, seeds an oracle-style `check-result.json`, and
executes the shipped `validate.py`.

Observed (2026-08-13, Harbor 0.21.0):

```
criteria_count=11
criteria=behavior_in_task_description,behavior_in_tests,informative_test_structure,anti_cheating_measures,structured_data_schema,pinned_dependencies,typos,tests_or_solution_in_image,test_deps_in_image,hardcoded_solution,file_reference_mentioned
assembled_tests=['criteria.json', 'test.sh', 'validate.py']
validator_exit=0
missing criterion: behavior_in_task_description
validator_rejects_incomplete_exit=1
OK: default rubric loaded; assembled wrapper; validator accepted oracle-seeded result
```

Full transcript: `captures/check/demo.log`. No model was invoked.

## Verdict

**Adopt into brief 07 (canary suite) and 11 (migration).** Gate every newly
registered or migrated task through this rubric before it can be pinned. Run
the live evaluator only as queued billable work under brief 05; keep the
stdlib validator as a pre-flight so a malformed check cannot look like a
quality pass. Not needed for brief 08/10.
