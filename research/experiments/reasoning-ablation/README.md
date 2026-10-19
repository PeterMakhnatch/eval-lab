# Reasoning ablation: full trace vs actions-only for the QA Trace judge (2026-10-19)

Track B, LLM arm. Same blinded test items, same judge, two views: the full
normalized trajectory (agent messages and `reasoning_content` kept) and the
paired actions-only view (tool calls and observations; agent prose/reasoning
removed). Views come from [PR #1083](https://github.com/PeterMakhnatch/eval-lab/pull/1083)
(external keys) and PR #1087's internal materialization
(`eval-lab-results/2026-10-19/internal-traces/`).

**System `qa-trace/6-or1p`.** Night PK-1 left the 6/7/8 comparison blocked and
kept qa-trace/6 provisionally, so the judge text is qa-trace/6's T1 question
texts and guardrail/disconfirmation block, loaded verbatim from `556170ab`
(hash-checked). Execution differs from the agentic pipeline, identically for
both views: one call per (item, view) with the trajectory inline (100k-char
head/tail budget), no tools, no T0 prescan, no T3 refuter, qa-trace/6 skip rule.
Route: `openrouter-metered/openai/gpt-oss-120b`, DeepInfra bf16 pinned, no
fallbacks, reasoning medium. Protocol: [`freeze.json`](freeze.json), committed
before any test call. Seals: [`seal.json`](seal.json), [`seal-full-rerun.json`](seal-full-rerun.json),
committed before scoring.

Positive = `expected_verdict == fail`. Primary heads were pre-registered:
`any_finding` (TJ, paired), `reward_not_real` (MiMo passes), `reward_hacking`
(CheatBench, HackTrace). Registered exposures for `qa-trace/` systems are
applied at scoring (15 TJ test items from `qa-trace-tj-prompt-dev` are left
unmeasured: 173/188). CheatBench is prior-exposed (combined_v1 calibration);
none of these keys is a fresh holdout for QA Trace except HackTrace.

## Results (Wilson 95%; paired McNemar exact; bootstrap 95% of actions − full)

| Key | Head | View | Recall | 95% CI | False alarms | 95% CI |
|---|---|---|---:|---:|---:|---:|
| trajectory-judge-400 | any_finding | full | 65/127 = 51.2% | 42.6–59.7% | 0/46 = 0.0% | 0.0–7.7% |
| trajectory-judge-400 | any_finding | actions | 65/127 = 51.2% | 42.6–59.7% | 0/46 = 0.0% | 0.0–7.7% |
| trace-paired-v1 (11/27 available) | any_finding | full | 5/8 = 62.5% | 30.6–86.3% | 2/3 = 66.7% | 20.8–93.8% |
| trace-paired-v1 (11/27 available) | any_finding | actions | 5/8 = 62.5% | 30.6–86.3% | 1/3 = 33.3% | 6.2–79.2% |
| mimo-v7-canonical-pass70 | reward_not_real | full | 0/6 = 0.0% | 0.0–39.0% | 6/64 = 9.4% | 4.4–19.0% |
| mimo-v7-canonical-pass70 | reward_not_real | actions | 0/6 = 0.0% | 0.0–39.0% | 17/64 = 26.6% | 17.3–38.5% |
| cheatbench-actions-v1 (prior-exposed) | reward_hacking | full | 21/75 = 28.0% | 19.1–39.0% | 6/75 = 8.0% | 3.7–16.4% |
| cheatbench-actions-v1 (prior-exposed) | reward_hacking | actions | 27/75 = 36.0% | 26.1–47.3% | 2/75 = 2.7% | 0.7–9.2% |
| hacktrace-act-v1 | reward_hacking | full | 33/75 = 44.0% | 33.3–55.2% | 3/75 = 4.0% | 1.4–11.1% |
| hacktrace-act-v1 | reward_hacking | actions | 27/75 = 36.0% | 26.1–47.3% | 2/75 = 2.7% | 0.7–9.2% |

| Key | Class | Full-only flags | Actions-only flags | Δ actions − full | Bootstrap 95% | McNemar p |
|---|---|---:|---:|---:|---:|---:|
| trajectory-judge-400 | recall | 10 | 10 | +0.0 pp | −7.1 to +7.1 pp | 1.0 |
| trace-paired-v1 | false alarms | 1 | 0 | −33.3 pp | −100 to 0 pp | 1.0 |
| mimo-v7-canonical-pass70 | false alarms | 3 | 14 | **+17.2 pp** | +6.2 to +29.7 pp | **0.013** |
| cheatbench-actions-v1 | recall | 6 | 12 | +8.0 pp | −2.7 to +18.7 pp | 0.24 |
| cheatbench-actions-v1 | false alarms | 5 | 1 | −5.3 pp | −12.0 to 0.0 pp | 0.22 |
| hacktrace-act-v1 | recall | 8 | 2 | −8.0 pp | −16.0 to 0.0 pp | 0.11 |
| hacktrace-act-v1 | false alarms | 2 | 1 | −1.3 pp | −6.7 to +2.7 pp | 1.0 |

**Same-view noise floor** (secondary, declared before running): the full view
re-run once, sealed, then compared. Items whose primary-head decision differs:

| Key | Full vs actions | Full vs full re-run | McNemar p (paired flips) |
|---|---:|---:|---:|
| trajectory-judge-400 | 20/173 | 12/173 | 0.10 |
| trace-paired-v1 | 3/11 | 2/11 | 1.0 |
| mimo-v7-canonical-pass70 | 17/70 | 3/70 | **0.0001** |
| cheatbench-actions-v1 | 24/150 | 13/150 | **0.035** |
| hacktrace-act-v1 | 13/150 | 9/150 | 0.29 |

Re-run recall: TJ 67/127, CheatBench 18/75, HackTrace 30/75 (full: 65, 21, 33).

## Reading

- Detection of labelled positives mostly survives removing the agent's
  reasoning: TJ recall is unchanged, CheatBench recall is not lower, and the
  HackTrace drop (−8 pp) is within the paired interval and near the re-run noise.
- The clear effect is on real passes: without the agent's stated intent, the
  judge reads legitimate test-expectation updates as test manipulation, and
  false alarms on canonical MiMo passes rise from 9.4% to 26.6%. The same
  direction holds on the 37 items not truncated in either view (1/37 → 8/37).
- Neither view caught any of the 6 MiMo wrong passes; trace-only judging
  does not detect these wrong submissions.

## Fresh window-3 (blind; `mimo-fresh-window3`, eval-lab #1097)

This key was registered after the main run and scored once with the same frozen
protocol (`run.py --keys mimo-fresh-window3`). No recorded reward was given,
because the key mixes passes and non-passes. The primary head, `any_finding`, was
declared before any call. Predictions were sealed before scoring
([`window3-seal.json`](window3-seal.json)), with results in
[`window3-results.json`](window3-results.json). One not_a_pass item stayed
unparsed in both views, so 51 of 52 items are scored.

| Head | View | Recall (31 not_a_pass + 4 wrong_pass) | False alarms (17 real passes) |
|---|---|---:|---:|
| any_finding | full | 15/34 = 44.1% (28.9–60.6%) | 8/17 = 47.1% (26.2–69.0%) |
| any_finding | actions | 17/34 = 50.0% (34.1–65.9%) | 10/17 = 58.8% (36.0–78.4%) |

Paired: recall 3 full-only vs 5 actions-only flags (p = 0.73); false alarms 2 vs 4
(p = 0.69). Wrong passes flagged: 0/4 full, 1/4 actions. `reward_hacking` on real
passes: 2/17 full, 4/17 actions, the same direction as the MiMo pass70 rise but
with a small n. Spend $0.0979.

## Limits

- CheatBench full views are truncated in 110/150 items (actions: 106) and MiMo
  in 31/70 (actions: 13); reasoning text consumes budget in the full view.
- gpt-oss-120b sampling is stochastic; the re-run gives one noise sample.
- Transport (HTTP 429) double failures were re-dispatched label-blind before
  sealing (28 primary, 24 re-run); superseded rows are kept in the artifact dir.
- Spend: $0.9519 metered (OpenRouter `usage.cost`), 1,907 requests including the
  8-call dev smoke.

## Reproduce

```sh
D=~/Developer/eval-lab-results/2026-10-19/reasoning-ablation
uv run python research/experiments/reasoning-ablation/run.py manifest --out $D/test
keys run -- uv run python research/experiments/reasoning-ablation/run.py run --out $D/test
uv run python research/experiments/reasoning-ablation/run.py seal --out $D/test
uv run python research/experiments/reasoning-ablation/score.py --out $D/test \
  --replicate $D/test-rep-full --write $D/test/results.json
```

`predictions.compact.jsonl` holds label-free per-item answers and heads for both
runs (rationales stay in the artifact directory).
