# TB4 html-js-filter canary pair: DSH vs mini-swe-agent

One exploratory canary pair on `terminal-bench/html-js-filter`
(dataset `terminal-bench/terminal-bench@4.0.0`), not a ranking: with
n=1 per arm no comparative claim is supported. The baseline arm runs
`mini-swe-agent`; the candidate arm runs
`evallab.harbor_dsh:DeepSeekHarnessAgent` (referenced by string; the
compiler never imports it) with `reasoning_effort=max`. Both arms pin
model `deepseek/deepseek-v4-flash`, `attempts=1`, and a 1800s agent
timeout (the first-canary override, not craft's flat 28800s).

## How to compile

```sh
.venv/bin/python research/experiments/tb4-dsh-pair/compile_pair.py \
  --stage canary --out /tmp/tb4-dsh-pair
```

This writes two `ExperimentSpec` JSON files plus `pair-metadata.json`
into `--out`, then fails closed with a `ValueError`: TB4 tasks are not
registered in `library/registry/` and the TB4 checkout lives outside
the repo, so no repo-relative `task_path` exists to embed. The frozen
documents are still written. Fields `ExperimentSpec` cannot express
(`declared_variable: agent_name`, `mode: exploratory`, per-arm agent
kwargs, approval terms) live in `pair-metadata.json`, never as invented
schema fields. No TB4 task content is copied anywhere.

## Submission

`Integration` submits this pair only after Peter's approval, once the
task is registered or vendored. Until then the compiler keeps refusing.
