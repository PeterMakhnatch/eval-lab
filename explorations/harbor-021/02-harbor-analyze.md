# harbor analyze — trajectory rubric

## What it is

`harbor analyze <trial-or-job>` wraps each trial as an ephemeral Harbor job.
An evaluator (default `claude-code` / `claude-haiku-4-5`) reads the trajectory
and writes `analysis.json`. The shipped verifier
(`harbor/analyze/analyze-task-template/tests/validate.py`) requires a non-empty
`summary` plus one `{outcome, explanation}` per rubric criterion.

Default rubric (`harbor/analyze/prompts/analyze-rubric.toml`) has two checks:
`reward_hacking` and `task_specification`. The lab's analysis loop
(`docs/analysis-loop.md`, prompts 01–03) is a separate, custom sidecar
pipeline — it does not call this CLI.

## Demo

```bash
bash explorations/harbor-021/demos/run-analyze.sh
```

Uses the evidence trial
`evidence/runs/event-summary-oracle-evidence/event-summary__FZg7pvq` and
`assemble_analyze_task()` + the shipped validator. No model.

Observed (2026-08-13):

```
DEFAULT_ANALYZE_RUBRIC_PATH=.../harbor/analyze/prompts/analyze-rubric.toml
trial_dir=.../event-summary__FZg7pvq
criteria_count=2
criteria=reward_hacking,task_specification
response_schema_keys=['summary', 'checks']
assembled_tests=['criteria.json', 'test.sh', 'validate.py']
uploaded_trial=True
validator_exit=0
validator_rejects_incomplete_exit=1
OK: default analyze rubric loaded; assembled wrapper; validator accepted oracle-seeded result
```

Full transcript: `captures/analyze/demo.log`.

## Verdict

**Adopt into brief 09 (judge calibration), queued through brief 05.** The
default reward-hacking screen is cheaper than a custom judge and should run on
canary (07) and billable trials before they enter calibration. Do **not**
replace prompts 01–03 with `harbor analyze` — the lab already owns a
deterministic sidecar contract. Live analyze is billable; never invoke it
ad-hoc from RECON/CURATOR.
