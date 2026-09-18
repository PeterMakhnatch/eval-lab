# DSPy lane: typed judge calibration and typed harness components

Overnight DSPy work (2026-09-16) on GLM 5.3 Flash through the Lab's admitted
Z.ai Coding Plan route (`lm.py`, same credential path as the GEPA proposer).
Everything here runs on the host; nothing here starts Harbor or a paid agent trial.

## Setup

```bash
uv sync --frozen
uv pip install -r research/experiments/dspy/requirements.txt   # dspy 3.3.1 + litellm pin, project deps unchanged
export PYTHONPATH=research/experiments/dspy
```

`lm.py` disables GLM's hidden thinking by default: with it on, the model can
spend the whole `max_tokens` budget in `reasoning_content` and return an empty
answer, which DSPy reports as an adapter parse failure. `dspy.ChainOfThought`
supplies visible reasoning instead. It also materialises LiteLLM before any
thread pool starts (DSPy's lazy import races under `dspy.Evaluate`).

## Judge calibration (`judge/`)

Program: `evallab.calibrate.build_dspy_program()` — `family, rubric_json, document ->
judgments: dict[dimension, dict[criterion, JudgeCriterionVerdict]]`, typed so a
missing criterion or a non yes/no verdict fails at parse time. Metric: exact
per-criterion agreement with the sealed keys (`evallab.calibrate.dspy_metric`).
GEPA additionally receives the key's one-line rationale for every disagreeing
cell (`judge/metric.py:agreement_with_feedback`).

Splits are the Lab's frozen `split_dspy_examples`: 12 train / 4 val / 6 held-out
per family. A record over a family the optimizer saw is contaminated and is
**not** written; clean records are the unoptimized baselines and cross-family
transfer (optimize on A, score all 22 documents of B).

```bash
python -m judge.run --threads 3 baseline --family checkout-pool-exhaustion --run-id baseline
python -m judge.run --threads 3 optimize --train-family checkout-pool-exhaustion --optimizer gepa --budget light --run-id gepa-checkout
python -m judge.run --threads 3 evaluate --program judge/artifacts/gepa-checkout/program.json \
    --run-id gepa-checkout --family checkout-pool-exhaustion --split heldout --family retry-storm-backlog
uv run evallab calibrate retry-storm-backlog --predictions research/experiments/dspy/judge/artifacts/gepa-checkout/bundle-retry-storm-backlog.json --skip-catalog
python -m judge.run export-sft --program judge/artifacts/gepa-checkout/program.json --family checkout-pool-exhaustion --run-id gepa-checkout
```

`--train-family both` trains on both families' train/val splits (held-out
documents of both families stay unseen). Optimizers: `bootstrap`, `bootstrap-rs`,
`mipro`, `gepa`, `simba`; GEPA's budget is an explicit metric-call cap
(`light`=120, `medium`=240, `heavy`=480), roughly $0.0015 per call at Flash prices.

Results live in `judge/RESULTS.md`; every number there has a `judge/artifacts/<run>/metrics-*.json`
and, for full-family runs, a `bundle-*.json` plus a record under
`research/calibration/records/<family>/`. The Z.ai Coding Plan tolerates about
3 concurrent requests; 8 produced rate-limit failures that `evaluate_set` now
retries sequentially and refuses to record if they persist.

`export-sft` writes the data half of `dspy.BootstrapFinetune` (BetterTogether,
arXiv:2407.10930): teacher traces above a score threshold formatted as chat
messages per predictor call. It names no trainer and trains nothing.

## Typed harness components (`harness/`)

`TaskDecomposer` (instruction + environment listing -> `ExecutionPlan`) and
`ErrorRecovery` (`goal, last_action, exit_code, error_output, recent_history ->
RepairStrategy` with a closed `kind` enum). `harness/offline_eval.py` checks
them against retained material only: Lab task instructions under `library/tasks`
and 17 real failure steps extracted from retained codex ATIF traces
(`harness/fixtures/recovery-steps.json`). These are typed-validity and
consistency checks, not evidence that either module improves an agent; that
would need a Harbor comparison through the Lab's queue.
