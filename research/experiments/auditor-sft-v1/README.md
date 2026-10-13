# Auditor SFT v1 — Terminal Wrench exploit-history LoRA

## Status (2026-10-13 04:40 AM ET)

- Corpus, budget guard, native evaluation staging, and one free locked-Docker
  control are complete.
- The single approved Tinker training run reached step 601/633
  (602 logged batches, progress 0.9495; train NLL 1.052 → 0.518), then Tinker
  returned HTTP 402 billing-blocked, paused for 3,605 s, and aborted.
- No checkpoint exists: the run saved final-only (`save_every=0`) and never
  reached the final save. `list_user_checkpoints` returns `[]`, and the SDK
  has no reattach-by-training-run operation, so the partial LoRA is not
  recoverable from this checkout.
- No paid evaluation trial was submitted. All paid work is stopped.

## Corpus

`prepare_data.py` pins `few-sh/terminal-wrench`
`d8a29613235a0ef56a8b70b3142626a533da28c2`, selects 200 rewarded
serious-exploit Terminus histories (139 task/instruction groups), and excludes
the five held-out task IDs and their instruction hashes before converting.
The source cache is sealed by `revision.receipt.json`; a changed revision or
drifted index refuses instead of relabeling cached bytes.

Assistant turns are declared derived structured-action behavior-cloning
targets, not raw teacher emissions: verbatim Analysis/Plan prose plus verbatim
recorded tool calls are rendered as native Terminus JSON and gated through the
installed Harbor stock parser. Because `qwen3_5_disable_thinking` lacks the
renderer extension property, `conversations.jsonl` contains one exact prefix
per assistant turn (634 supervised examples) for
`train_on_what=last_assistant_message`; `histories.jsonl` retains the 200 full
histories and `examples.jsonl` maps every prefix to its history and ATIF step.

Offline measurement with the pinned tokenizer/renderer found 2,286,387
rendered tokens per epoch, 260,605 supervised tokens, longest example 14,512
tokens, and zero truncations at `max_length=16384`.

## Budget and spend

Peter authorized a hard $15 Tinker cap for the whole job and no $40 L1 test.
The frozen plan in `derived/auditor-sft-v1/budget-plan.json` bounds:

| Item | Ceiling (USD) |
| --- | ---: |
| Training, 2 epochs at $1.177/M rendered tokens | 5.382155 |
| Evaluation reserve, 20 trials × $0.25 native cap | 5.00 |
| Private checkpoint storage reserve, 24 h | 2.25 |
| Whole job | 12.632155 |

`spend.jsonl` records a $0 native preflight refusal (the launcher had created
an empty log directory that cookbook `chat_sl` rejects before any
ServiceClient/model call; fixed in `src/evallab/sft_tinker.py`) and the billing
abort. Logged batches consumed 4,316,266 tokens = $5.080246 at the published
price; the provider invoice has not been observed and an in-flight batch may
add charges, so the full $5.382155 training reservation remains charged
against the $15 cap. A fresh full retrain plus the original evaluation plan
would therefore need a new cap/scope decision from Peter after billing is
restored; this lane must not silently shrink epochs, tasks, or trials.

## Evaluation staging

`holdout_manifest.json` freezes the five held-out TW tasks and all 56 original
file hashes. `prepare_eval.py` stages native queue specs (never submits):

- five held-out TW tasks with the exact tracked TW hack prompt v5;
- five historical leaky MiMo packages with Eval Lab exploit-probe-v2;
- base `tinker/Qwen/Qwen3.6-35B-A3B`, locked Docker, 64 requests,
  400k input / 16,384 output tokens, $0.25 cost limit per trial;
- LoRA specs only when `--lora-sampler-path` carries a real checkpoint URI;
- free `nop` and cheat-ladder control commands for every task.

One free control has run: Titanic `nop` under the creation-time egress lock
completed with reward 0, no exception, `egress-lock.json: applied=true`, and a
healthy verifier (`Missing solution.csv`, score 0.0). It proves the locked
path and grading, not Terminus/tmux on the opaque images or model behavior.

## Reproduction (free steps)

Use this worktree and a checkout-owned derived root:

```bash
export EVALLAB_DERIVED_ROOT="$PWD/derived/parquet"
uvx --from uv==0.9.24 uv run --no-sync python \
  research/experiments/auditor-sft-v1/prepare_data.py prepare \
  --index-dir derived/auditor-sft-v1/source-cache/index \
  --cache-dir derived/auditor-sft-v1/source-cache \
  --out derived/auditor-sft-v1/data \
  --holdout derived/auditor-sft-v1/holdout.json
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 uvx --from uv==0.9.24 uv run --no-sync \
  python research/experiments/auditor-sft-v1/train.py plan
uvx --from uv==0.9.24 uv run --no-sync python \
  research/experiments/auditor-sft-v1/prepare_eval.py --prepare \
  --out derived/auditor-sft-v1/eval-v2
```

Do not run `train.py train`, submit/approve/tick specs, or start more paid
work until Peter restores Tinker billing and decides the retrain budget/scope.
The shipped recipe now saves periodic checkpoints every 100 steps under the
24 h lease in `tools/tinker-sft/checkpoint_lease.py`, so an authorized retrain
cannot lose 95% of training to a late billing abort the way the final-only
run did. The failed run's exclusive `training-intent.json` remains in place,
so a retrain still requires deliberate operator cleanup and a fresh plan.
