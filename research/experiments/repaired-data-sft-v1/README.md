# repaired-data-sft-v1: does training on repaired MiMo passes beat training on the originals?

This is the overnight experiment of 2026-10-16 (FOCUS "Overnight plan 2026-10-16"). The
Training owner is the Classifiers tab. The cap is $11, logged in `spend.jsonl` under ledger
tag `2026-10-16`.

## Question and arms

Does a model trained on passes that survive repaired graders solve held-out tasks better,
and produce fewer incorrect passes, than the same model trained on the original passes?

| arm | data | role |
|---|---|---|
| A | every recorded ordinary MiMo pass (QA Trace `build_arms.py`, #936) | reference at natural size |
| B | A rows whose qualified repaired-v6 regrades all pass and that carry no defect flag | primary treatment |
| C | seeded random sample of A, same size as B | primary control (quality isolated from quantity) |

**Primary comparison:** B vs C at equal example count (Research decision, 01:30 ET). B is
never padded.

## Base model and recipe (identical for every arm)

**Base model**
- `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` @ `2367e865d009c13ac81713a2878291d33ab28177`, bf16.
- It produced 66 of the 70 recorded passes, so this is filtered self-distillation.
- Tinker could not be used: it returned HTTP 402, billing-blocked.

**Trainer settings** (`tools/modal-mimo-sft/sft.py`, one A100-80GB)
- LoRA r16, α32, dropout 0.05 on q/k/v/o/gate/up/down.
- lr 5e-5, cosine schedule with 3% warmup.
- batch 1 × grad_accum 4, 1 epoch, seed 42.
- Loss on every assistant turn, reasoning included.
- max_length 65,536; over-length rows are refused, never truncated.

**Why grad_accum is 4, not the HAR-129 16:** with 16, an arm of 16 rows or fewer gets a
single optimizer step, and the first warmup step has learning rate 0.

## Data path

`to_export.py` consumes `build_arms.py` output.

**Lossless conversion (`all/`).**
- Every A row is converted, and the result must render byte-identically to the chat
  template's render of the recorded row:
  - mimoagent tool calls become the template's `<tool_call>` text;
  - the `tools` schema becomes the template's system turn;
  - tool results stay `tool` turns.
- Provenance is written line-aligned to `provenance.jsonl`.
- All 70 of the 2026-10-16 rows load through `sft.verify_export`.
- `sft.py` gained the template's `tool` role for these rows. Its turns never carry loss,
  and every prefix and mask is still verified.

**Training eligibility.** One rule applies to every arm, before C is drawn:
- `fidelity == raw_completion`;
- render ≤ 65,536 tokens.

On the 70 rows this drops 2 reconstructed rows and 6 over-length rows, leaving 62 rows over
38 tasks (2.42M tokens). C is then redrawn as a seeded sample of eligible A, with
|C| = |eligible B|.

## Held-out evaluation

- **Held-out tasks.** MiMo data picks them, disjoint from all 42 task ids in A (option (a),
  Research 01:55 ET). No arm is scored before that manifest exists and disjointness is
  checked.
- **Server.** All arms and the base are served by one `serve_lora.py` server:
  `EVALLAB_MIMO_LORAS=rds1-a=…,rds1-b=…,rds1-c=…`, with selectors
  `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B[:rds1-a|:rds1-b|:rds1-c]`.
- **Trials.** They run on Infra's Modal rollout runner with the same settings for every
  arm.
- **Reported per arm:**
  - example count;
  - held-out solve rate with an exact (Clopper–Pearson) 95% interval;
  - incorrect-pass rate;
  - a paired exact McNemar test for B vs C on the same tasks.

## Runs

| run | data (conversations sha256) | rows | seq tokens | estimate | billed |
|---|---|---:|---:|---:|---:|
| `rds1-smoke` | 2 rows of A (smoke only; never evaluated) | 2 | 17,255 | $0.02 + startup | ≈$0.11 (wall time × rate) |
| `rds1-a` | `c876bc07d63aab875107ede76f86c58f0a840942aab9ee6497c613f271a03efb` | 62 | 2,417,765 | $3.38 | pending |
