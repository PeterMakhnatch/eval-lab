# Job plugin API (`--plugin`)

## What it is

Harbor jobs accept `--plugin module:ClassName` (or a registered entry-point
name). The class subclasses `harbor.models.job.plugin.BaseJobPlugin` and
implements `on_job_start(job)` / `on_job_end(job_result)`. From
`on_job_start` it can subscribe to trial hooks (`job.on_trial_started`,
`on_environment_started`, `on_agent_started`, `on_verification_started`,
`on_trial_ended`).

Reference implementation: `~/Developer/agent-evals/harbor/packages/harbor-langsmith`
(`LangSmithPlugin`). It syncs a dataset, opens an experiment session, and
POSTs trial/phase runs to LangSmith — a networked backend the lab will not
call. The same hook surface is how `harbor-atif2otel`'s `OtelPlugin` would
ship traces (brief 08).

`harbor plugins list` on this machine: none installed.

## Demo

```bash
bash explorations/harbor-021/demos/run-plugin.sh
```

Local plugin: `demos/file_hook_plugin.py` (`FileHookPlugin`). Writes
`hooks.jsonl` on start/end. Attached with:

```bash
PYTHONPATH=explorations/harbor-021/demos \
  harbor run --path tasks/event-summary --agent oracle \
  --plugin file_hook_plugin:FileHookPlugin \
  --pk output_dir=explorations/harbor-021/captures/plugin \
  --jobs-dir runs --job-name plugin-oracle-demo -n 1
```

Observed (2026-08-13): job reward **1.000**, 8s, no exceptions. Hook log:

```
{"event": "on_job_start", "job_name": "plugin-oracle-demo", "n_tasks": 1}
{"event": "trial_event", "trial_event": "start", "trial_name": "event-summary__LoHW4aD"}
{"event": "trial_event", "trial_event": "end",   "trial_name": "event-summary__LoHW4aD"}
{"event": "on_job_end", "n_total_trials": 1, "n_completed": 1, "n_errored": 0}
```

No LangSmith, no network. Transcript: `captures/plugin/demo.log`.

## Verdict

**Adopt into brief 05 (executor) and 08 (Phoenix).** Use a local plugin to
append `events.jsonl` / cost hooks from Harbor's own trial lifecycle instead
of scraping job dirs after the fact. For 08, install `harbor-atif2otel` as
the `--plugin atif2otel` once Phoenix is up — do not adopt LangSmith.
`skip` LangSmith itself because it is a paid/cloud backend the stack already
replaced with Phoenix.
