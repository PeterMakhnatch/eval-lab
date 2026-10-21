# seeker-sft-loss-target-20261021

pE-SELF-6 data-quality ablation. pE-SELF-5 trained `Qwen/Qwen3-8B` on
`seeker_tool_sft.jsonl` with the loss over the **whole** rendered conversation and
the model learned the submission contract but not exploit discovery. This
experiment tests the obvious data-quality hypothesis: most of the corpus is text
the model must only *read*, so the decision signal is diluted.

## Hypothesis

The corpus is dominated by tokens the model should never generate — the Seeker
system prompt (identical in all 90 conversations), the task instruction, and the
`ConfinedTools` observations. Training on them spends gradient on predicting text
that is given at inference time, and it teaches the model to imitate *reading*
rather than *deciding*. Masking the loss to assistant tokens is the standard SFT
target and should therefore produce a better seeker at the same data scale.

## Measured dilution (`validation.json`, free CPU)

| Role | Tokens | Share |
| --- | ---: | ---: |
| assistant | 110,007 | 47.2% |
| system | 91,710 | 39.3% |
| tool | 19,280 | 8.3% |
| user | 12,139 | 5.2% |
| **total** | **233,136** | |

Per conversation the assistant share runs `0.27`–`0.91` (mean `0.376`). So the
full-token loss spends ~53% of its gradient on tokens that are pure context at
inference time, and the system prompt alone is 39%.

## Arms

Same data, same seed, same LoRA config, same generation protocol; only the loss
target differs.

| Arm | Loss |
| --- | --- |
| `full` | every token (reproduces pE-SELF-5 inside this run) |
| `assistant` | assistant tokens only; everything else is `-100` |

The assistant spans are recovered by tokenizing the conversation rendered up to
and including each message and taking successive boundaries, because the Qwen3
chat template does not expose `return_assistant_tokens_mask`
(`pilot_modal.py --mode validate` in the pE-SELF-5 experiment recorded
`assistant_mask_supported: false`). `validate_data()` asserts that the boundaries
reconstruct the full rendering exactly.

## Protocol

- Training: `2` epochs, `lr=1e-4`, AdamW, grad-accum `4`, `bf16`, gradient
  checkpointing, `max_length=8192`, `A100-40GB`.
- Generation: the `evallab.seeker` system/user prompt and `ConfinedTools` verbatim,
  ≤`8` turns, greedy, `max_new_tokens=768`.
- Tasks: the same 6 TB4 DEV tasks as pE-SELF-5 — `4` memorization
  (`cargo-flight-dispatch`, `fin-saccr-rwa`, `html-js-filter`,
  `vf2-speedup-networkx`, whose confirmed `1/0` witnesses are in the corpus) and
  `2` strictly unseen (`uefi-bootkit`, `vpp-loss-divergence`).
- Scoring: `replay_pilot.py --results loss-target-results.json --out-root <dir>`
  — strict packaging gate, then `evallab.candidate_runner` upstream on the Modal
  backend, and the PM port only when upstream measures `1.0`.

**Reproduction** = a confirmed `1/0` on a memorization task (the model saw that
task's exploit). **Generalization** = a confirmed `1/0` on a transfer task.

## Generation budget (found while running the ablation)

pE-SELF-5 generated with `max_new_tokens=768`. The corpus's own
`submission/solve.sh` bodies run to `3574` characters (~`1021` tokens) at the
maximum, so a `write_file` call whose body exceeds the budget is cut off
mid-JSON: the closing `</tool_call>` never arrives, the call does not parse, the
turn is treated as "no tool call", and the loop nudges instead of writing. The
`full` arm at `768` shows exactly this — `5/6` runs burn all `8` turns re-emitting
the same truncated `write_file` and finish with an empty submission.

The ablation therefore runs at `3072` tokens per turn, which covers the corpus
maximum with headroom. `loss-target-results-768.json` keeps the `768` run as the
evidence for this finding.

## Results (`768` budget, `loss-target-results-768.json`)

| Arm | finished | claimed exploits | strict-valid | upstream measured | upstream `1.0` | confirmed `1/0` |
| --- | --- | --- | --- | --- | --- | --- |
| `full` | 1/6 | 0/6 | 1/6 | 0/6 | 0/6 | 0/6 |
| `assistant` | 6/6 | 0/6 | 6/6 | 6/6 | 0/6 | 0/6 |

The two arms fail in opposite ways, and both are visible in the traces:

- `full` is **erratic**: `5/6` runs emit a `write_file` whose body exceeds `768`
  tokens, so the call is truncated mid-JSON, never parses, and the loop burns all
  `8` turns re-emitting it. Only `html-js-filter` finishes, and the `5` truncated
  runs are refused by the runner for having no submission bundle at all.
- `assistant` is **uniform**: every run takes exactly `4` turns and `6` tool calls
  (`list_files` → `read_file` ×2 → `write_file` ×2 → `finish`) and writes a
  ~`100`-character "no reliable shortcut found" submission with
  `exploit_found: false`. All `6` are strict-valid and all `6` measure `0.0`
  upstream.

So masking the loss to assistant tokens produced a *stable, conservative policy*
at the same data scale — the first configuration that yields a complete, valid
submission in every run — but not exploit discovery. Neither arm claims an
exploit, and neither reproduces a training exploit.

## Reproduction (`reproduction_analysis.py`)

Best `difflib` similarity between a run's `submission/solve.sh` and the training
corpus's own `submission/solve.sh` bodies for that task (memorization tasks only):

| Arm | mean | max | runs > 0.5 |
| --- | ---: | ---: | ---: |
| `base` (pE-SELF-5) | 0.071 | 0.092 | 0 |
| `full` (pE-SELF-5 trained) | 0.174 | 0.294 | 0 |
| `full` (`768`) | 0.066 | 0.263 | 0 |
| `assistant` (`768`) | 0.236 | 0.278 | 0 |

The masked arm is the closest to the training exploits of any arm measured, but
no arm reaches even half the similarity of a reproduction: the corpus's exploit
*content* is not being reproduced by any configuration tried so far.

## Status of the ablation

The `3072` re-run was **stopped at the lane's Modal cap** before it returned
results, so the clean same-budget `full`-vs-`assistant` comparison is not
available. What the `768` run establishes is the budget confound itself: pE-SELF-5
measured `0/12` confirmed with a generator that silently truncated any submission
longer than `768` tokens, so its negative result is confounded for exactly the
runs that tried to write a real exploit.

## TB4 TEST leak check (`leak_check.py`, `leak-check.json`)

`research/answer-keys/tb4-audit-v1/tasks.jsonl` is the authoritative split: `41`
`test` tasks, `25` `dev` tasks. Checked on 2026-10-21 after the Research lane
reported that `seeker-gym-v0` mislabels `41` TEST tasks as dev:

| Check | Result |
| --- | --- |
| Training task ids across `research/datasets/seeker-sft-v1/*.jsonl` | `723` ids, `0` TEST hits |
| Pilot/ablation tasks | all `6` are `dev` |
| TEST task names or tokenless slugs in the dataset or either experiment's artifacts | `0` |

Matching is exact on task ids — a substring scan would flag the DEV task
`layout-config-recreation2` for containing the TEST task
`layout-config-recreation`, which the dataset already quarantines as an alias —
and the replay evidence's embedded upstream task packages are excluded because
that text is the task's own, not seeker training data.

## Modal spend (actual, `modal billing report --for today`)

| App | Purpose | Cost |
| --- | --- | ---: |
| `seeker-sft-pilot-20261021` (A100) | pE-SELF-5 train + generate | $0.652301 |
| `seeker-sft-pilot-20261021` (A10G, OOM) | pE-SELF-5 first attempt | $0.056006 |
| `seeker-sft-pilot-20261021` (validate ×2) | template/token checks | $0.002997 |
| `seeker-sft-loss-target-20261021` (`768`) | this ablation, both arms | $2.000882 |
| `seeker-sft-loss-target-20261021` (`3072`, stopped) | re-run stopped at the lane cap | $0.295857 |
| `seeker-sft-loss-target-20261021` (validate) | role/mask check | $0.000201 |
| `2026-10-21-seeker-sft-pilot` (replays) | `candidate_runner` upstream replays | $0.059443 |
| **Total** | | **$3.067687** |

All paid Modal runs are stopped; no further paid run was started after the cap
notice. The `3072` re-run was terminated mid-flight, so its results are lost and
its `$0.295857` is a sunk cost.

## Files

| File | Role |
| --- | --- |
| `loss_target_modal.py` | Modal app: `--mode validate` (token/role budget, mask check) and `--mode pilot` (both arms) |
| `validation.json` | role token budget and assistant-fraction report |
| `loss-target-results-768.json` | the `768`-budget run: training losses, traces, submissions |
| `replay-summary.json` | per-run and per-arm replay results for the `768` run |
| `reproduction_analysis.py` | similarity of generated submissions to the training exploits |
| `leak_check.py` | TB4 TEST leak check against `research/answer-keys/tb4-audit-v1/` |
| `leak-check.json` | the leak check's report |

## Commands

```bash
uv run modal run research/experiments/seeker-sft-loss-target-20261021/loss_target_modal.py --mode validate
uv run modal run research/experiments/seeker-sft-loss-target-20261021/loss_target_modal.py --mode pilot
uv run python research/experiments/seeker-sft-pilot-20261021/replay_pilot.py \
  --results research/experiments/seeker-sft-loss-target-20261021/loss-target-results.json \
  --out-root ~/Developer/eval-lab-results/2026-10-21/seeker-sft-loss-target
```
