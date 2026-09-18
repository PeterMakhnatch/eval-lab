# DSPy lane: typed judge calibration and typed harness components

DSPy work (2026-09-16) on GLM 5.3 Flash through the Lab's admitted Z.ai Coding
Plan route (`lm.py`, same credential path as the GEPA proposer). Everything here
runs on the host; nothing here starts Harbor or a paid agent trial.

## Setup

The DSPy overlay (`requirements.txt`: dspy 3.3.1, litellm 1.101.0, numpy pinned
to the lock) runs in uv's ephemeral environment layered over the locked venv.
It is never installed into the project venv, so `uv run evallab ...` keeps
running on exactly what `uv sync --locked` produced.

```bash
uv sync --frozen
export PYTHONPATH=research/experiments/dspy
alias dspy-run='uv run --with-requirements research/experiments/dspy/requirements.txt python'
```

`lm.py` disables GLM's hidden thinking by default: with it on, the model can
spend the whole `max_tokens` budget in `reasoning_content` and return an empty
answer, which DSPy reports as an adapter parse failure. `dspy.ChainOfThought`
supplies visible reasoning instead. It also materialises LiteLLM before any
thread pool starts (DSPy's lazy import races under `dspy.Evaluate`).

## Judge calibration (`judge/`)

Program: `evallab.calibrate.build_dspy_program()` — `family, rubric_json, evidence,
document -> judgments: dict[dimension, dict[criterion, JudgeCriterionVerdict]]`,
typed so a non yes/no verdict fails at parse time. `evidence` is the family's
evidence directory rendered verbatim (`research/calibration-evidence/<family>/`,
vendored byte-for-byte from the pinned harbor-practice task source with a
manifest of sha256s): the same files the postmortem author had, so
`invents_evidence`, `misstates_a_fact` and the other evidence criteria are
decided against the actual permitted evidence rather than against fixture names
an optimizer happened to memorise. `show-input` renders exactly what the model
receives for one document, with no model call:

```bash
dspy-run -m judge.run show-input --family checkout-pool-exhaustion --document 10-correct-timeline-dense
```

Contract failures are loud, never silent: a missing, edited or unlisted evidence
file raises `EvidencePackError` before any call; a judge output that omits any
criterion raises `IncompleteJudgeOutputError` naming every missing cell and no
bundle is written; a saved program compiled against the pre-evidence signature
is refused on load (`check_dspy_program_state`); a bundle or record carries the
`evidence_digest` it was judged against, and the digest reports a record scored
without the evidence pack as "pre-evidence … not calibration" even when it is
above the floor.

Metric: exact per-criterion agreement with the sealed keys
(`evallab.calibrate.dspy_metric`). GEPA additionally receives the key's one-line
rationale for every disagreeing cell (`judge/metric.py:agreement_with_feedback`).

Splits are the Lab's frozen `split_dspy_examples`: 12 train / 4 val / 6 held-out
per family. A record over a family the optimizer saw is contaminated and is
**not** written; clean records are the unoptimized baselines and cross-family
transfer (optimize on A, score all 22 documents of B).

```bash
dspy-run -m judge.run --threads 2 baseline --family checkout-pool-exhaustion --run-id baseline-evidence
dspy-run -m judge.run --threads 2 optimize --train-family checkout-pool-exhaustion --optimizer gepa --budget light --run-id gepa-checkout
dspy-run -m judge.run --threads 2 evaluate --program judge/artifacts/gepa-checkout/program.json \
    --run-id gepa-checkout --family checkout-pool-exhaustion --split heldout --family retry-storm-backlog
uv run evallab calibrate retry-storm-backlog --predictions research/experiments/dspy/judge/artifacts/gepa-checkout/bundle-retry-storm-backlog.json --skip-catalog
dspy-run -m judge.run export-sft --program judge/artifacts/gepa-checkout/program.json --family checkout-pool-exhaustion --run-id gepa-checkout
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
