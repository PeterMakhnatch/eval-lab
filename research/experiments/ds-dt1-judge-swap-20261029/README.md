# ds-dt1: DeepSeek-V4.1-Flash as the Stage-2 judge on CM-13's frozen facet-cited windows

DEV only: the Goodfire cohort is 80 enacted / 70 clean runs, admitted by the unchanged strict
`assignments.csv` cross-key inside the CM-13 tooling. No cross-test set is touched. Prompts and replies never
enter git; they live in `~/.local/state/evallab-prod-live/run-atlas/comparisons/cm20-deepseek/`.

The question is CM-19's, with the judge swapped to a different model family and a k-pass vote added: does the
facet-first arm C reach the preregistered rule (**recall >= 20/80 with <= 2/70 clean false alarms**) alone, or as a
majority of repeated judge calls? Comparators (published, not re-measured): terra 25/80 at 6/70, Opus 5.5 14/80 at
5/70, Sonnet 5.5 6/80 at 1/70.

## What is new relative to `cm19_opus.py`

`cm19_deepseek.py` is a copy of CM-19's tool with three changes, marked in its docstring:

1. **Route** — `deepseek/deepseek-flash`.
2. **Call mechanism** — the pinned `omp -p --mode json` transport is otherwise identical (one assistant turn,
   served-model attestation, fallback and tool-call refusal), but it drops the subscription-env scrub and the
   subscription-only route check, and installs a dollar `BudgetGate` instead of the Anthropic quota gate.
3. **Cost accounting** — `usage.cost.total` from every assistant record of every attempt is summed into the cache
   row and the spend ledger, so the run reports real USD instead of $0 for a subscription.

Prompts, plan, windows, arms, the CM-13 system prompt and the scorer are untouched. `run()` re-hashes
`plan.json` and `prompts.json` before judging and refuses if either moved.

## Files

| file | role |
|---|---|
| `cm19_deepseek.py` | `prepare` (copy + re-check the CM-13 sha256 pins), `run --jobs N --replicate K`, `score --replicate K` (the unchanged CM-13 scorer) |
| `vote.py` | k-of-n vote over the passes; run rows are built in the scorer's own shape so `facet_windows.aggregate` aggregates them |
| `length.py` | recall and clean false alarms inside run-length tertiles, plus the length-only `>= 41 steps` baseline |
| `compare.py` | window- and run-level agreement with the stored Opus (CM-19) and terra (CM-13) judgments, exact sign test, complete cases only |
| `attestation.py` | per-pass served-model enforcement, answer-schema counts, tokens and USD from the cache and the ledger |
| `results/` | `freeze.json`, `result-single.json`, `result-vote.json`, `result-r*.json`, `length-control.json`, `compare.json`, `attestation.json` |

## Reproduce

```bash
uv run python research/experiments/ds-dt1-judge-swap-20261029/cm19_deepseek.py prepare
uv run python research/experiments/ds-dt1-judge-swap-20261029/cm19_deepseek.py run --jobs 40 --replicate 1
uv run python research/experiments/ds-dt1-judge-swap-20261029/cm19_deepseek.py run --jobs 40 --replicate 2  # ... 5
uv run python research/experiments/ds-dt1-judge-swap-20261029/cm19_deepseek.py score --replicate 1
uv run python research/experiments/ds-dt1-judge-swap-20261029/vote.py --passes 5
uv run python research/experiments/ds-dt1-judge-swap-20261029/length.py
uv run python research/experiments/ds-dt1-judge-swap-20261029/compare.py
uv run python research/experiments/ds-dt1-judge-swap-20261029/attestation.py
```

A call counts only if the served `provider/model` equals `deepseek/deepseek-flash` and the reply has no tool call,
`stopReason == "stop"` and exactly one assistant turn; anything else is recorded as failed and scored as **unknown**,
never as a negative. The scorer's self-test — the unchanged CM-13 scorer on the stored CM-19 Opus cache — must
reproduce CM-19's published **arm C 14/80 at 5/70** before any of this is read; it does.
