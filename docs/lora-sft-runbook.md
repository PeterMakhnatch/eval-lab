---
status: living
audience:
  - runner
  - operator
---

# LoRA SFT runbook: train, serve base + adapter, check parity, cost

This page covers one LoRA SFT of `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` (revision `2367e865…`) on Modal, and how to serve it beside the base model for a paired eval. It was first run in HAR-129 (2026-10-01); the evidence is in `research/experiments/har129-lora/`. Every command runs from the repository root, in a worktree.

## 0. Before any paid step

```bash
evallab spend check --since <window start, e.g. 2026-10-01T04:00:00Z> --cap-usd <cap> --candidate-usd <estimate>
```

- **Exit 0:** the launch fits under the cap.
- **Exit 3:** it would exceed the cap. Do not launch.
- **Exit 2:** spend could not be verified (catalog or Modal unreadable, or an unratable queued spec). Do not launch.

`evallab spend day --date $(date -u +%F)` breaks down what a UTC day has already cost.

Without `--cap-usd`, both spend commands read `policy/standing-approvals.yaml`.
`spend day` covers a whole UTC day and resolves its cap at **00:00 UTC on the
reported date**. Every validated dated override expires after that instant,
so the override applies to its entire `utc_date` for reporting, even if it
expires during that day or the report is generated later. The output names
the dated card and standing ceiling, for example
`cap $35.00 (dated override HAR-126; standing $20.00)`.
Other dates use the standing ceiling.

`spend check` is a launch decision, not a historical day report: it resolves
the default ceiling at the **window end (now)** and respects override expiry.
An older `--since` does not revive an expired approval. An explicit
`--cap-usd` takes precedence in either command; reporting a past override
never renews authorization to spend.

Card attribution is explicit-only and fail-closed. A queued spec may declare
`linear_card: HAR-126`; the runner carries it through run provenance into the
catalog, and `spend day` attributes that job's Daytona/model rows to the card.
An explicit card that disagrees with the job-name prefix fails closed to
`unattributed` with a conflict note — task, model, harness, and app names are
never attribution. Modal billed cost splits by app (one row per billed app);
an app takes a card only through an explicit binding, otherwise it stays an
`unattributed` residual line. Jobs that predate the explicit field and carry no
HAR job-name prefix (e.g. the 2026-10-01 `ovn-g5-*` runs) stay `unattributed`
in the ledger; republishing with an explicit `publication_card` binds the
results home without rewriting raw records. Provider limits: the Daytona API
exposes quota snapshots only (no billed dollars, so Daytona rows stay
estimates), and Modal bills the account rather than jobs (per-job GPU shares
come only from an explicit HAR-131 session receipt).

## 1. Data

- **Input** is an `evallab.sft_terminus/1` export: a directory with `conversations.jsonl` and `manifest.json`, which carries `conversations_sha256`. Training refuses anything else.
- **Each row** holds `{"messages": [...]}` plus an optional `"loss": "last"`.
- **Loss on every assistant turn** (the default): reasoning is a leading `<think>\n…\n</think>\n\n` in an assistant turn's content.
- **Loss on the final assistant turn only** (`"loss": "last"`): use this for per-call samples whose history is exactly what the served model saw.
  - History assistant turns carry no reasoning, because the harness never sends reasoning back.
  - The final turn carries its reasoning in `reasoning_content`, not inline.
  - The target is exactly the served completion, `<think>{reasoning}</think>{content}<|im_end|>`, rendered after the `add_generation_prompt` prefix of the history.
- **Check offline first.** This is free: it renders every row with the model's own template and verifies the masks.

```bash
uv run --project tools/modal-mimo-sft --locked python tools/modal-mimo-sft/sft.py dry-run --data <export> --epochs 1
```

The output shows tokens per row, trained tokens, the longest row and a cost estimate. A row longer than `max_length` (default 65,536, the served context) is refused by label, never truncated. Post the totals line and the manifest sha256 before training.

## 2. Train (Modal, one A100-80GB)

```bash
uv run --project tools/modal-mimo-sft --locked python tools/modal-mimo-sft/sft.py train \
  --data <export> --run-name <run> --rank 16 --alpha 32 --lr 5e-5 --epochs 1 --confirm-spend
```

These are the HAR-129 G4 settings, and also the script defaults.

The command prints the estimate, then refuses unless `--confirm-spend` is passed. It then:
1. uploads the export to the `evallab-mimo-v26-9b-sft` volume;
2. audits the rows TRL prepared against the rendered rows: the same count and order, identical `input_ids`, and collated labels equal to the rendered mask. Any mismatch raises before step 1;
3. trains with these settings:
   - LoRA dropout 0.05, cosine schedule with 3% warmup;
   - batch 1 × 16 accumulation, seed 42;
   - gradient checkpointing;
   - a chunked loss over the selected tokens only;
4. writes `<run>/adapter` and `<run>/receipt.json`.

The receipt holds the export digests, the adapter sha256 per file, the loss history, peak memory, train seconds and the label audit counts.

```bash
uv run --project tools/modal-mimo-sft --locked modal volume get evallab-mimo-v26-9b-sft <run>/receipt.json receipt.json
```

## 3. Serve base + adapter on one server

```bash
EVALLAB_MIMO_LORA_ADAPTER=<run>/adapter EVALLAB_MIMO_LORA_NAME=har129 \
  uv run --project tools/modal-mimo-serve --locked modal deploy tools/modal-mimo-serve/serve_lora.py
```

The app `evallab-mimo-v26-9b-lora` uses production's SGLang image and launch command, plus the LoRA flags. The adapter is selected per request by the model name `<base>:<adapter>`.

| arm | queue selector | upstream |
|---|---|---|
| stock | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` | `EVALLAB_MIMO_SELFHOSTED_UPSTREAM=<lora server URL>` |
| tuned | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129` | same URL |

Only the adapter names in `execution_contracts.MIMO_SELFHOSTED_ADAPTERS` are admitted. A new adapter name is a code change: add it there, and to the proxy's `mimo_selfhosted` profile.

## 4. Check parity before the eval

```bash
uv run python research/experiments/har129-lora/smoke_parity.py prompts prompts.json <trajectory.json>...
uv run --project tools/modal-mimo-serve --locked modal deploy tools/modal-mimo-serve/serve.py   # production, if stopped
uv run --project tools/modal-mimo-serve --locked modal run tools/modal-mimo-serve/lora_smoke.py \
  --prompts prompts.json --lora-url <lora url> --prod-url <prod url> --adapter har129 --out raw.json
uv run python research/experiments/har129-lora/smoke_parity.py score raw.json scored.json
uv run --project tools/modal-mimo-serve --locked modal run tools/modal-mimo-serve/lora_logprob_probe.py \
  --prompts prompts.json --url <lora url> --adapter har129 --out logprob-probe.json
```

Pass criteria:
- the base name is byte-identical to production at temperature 0;
- every reply on both names is a valid Terminus turn (`json` or `normalized`);
- the adapter is applied: with `lora_logprob_probe.py`, base vs base and adapter vs adapter show 0 logprob difference, while base vs adapter shows a nonzero one. Greedy text alone can't show this, because a small adapter often leaves greedy text unchanged, and long greedy generations can diverge between identical requests.

The smoke requests run inside Modal, so the API key never leaves it.

## 5. Stop everything

```bash
uv run --project tools/modal-mimo-serve --locked modal app stop -y evallab-mimo-v26-9b-lora
uv run --project tools/modal-mimo-serve --locked modal app stop -y evallab-mimo-v26-9b
```

## Cost (measured 2026-10-01)

| step | rate / measurement | example |
|---|---|---|
| training | the estimator charges 560 sequence tok/s (about **$1.40 per 1M sequence tokens** per epoch). That figure was a first-step measurement, so it errs high | dry run: 143,829 tokens, one step, 319 s wall, $0.28. **G4: 1,569,955 tokens, 10 steps, 1,330 s train / 1,370 s wall (step 1: 558 s; later steps about 86 s), $1.27 actual vs $2.19 estimated** |
| serving | $2.8149/h while warm, plus about 3–5 min of cold start and a 5 min idle tail | dry-run smoke: about $0.37 for the LoRA server, about $0.26 for production. G4 smoke and probe: LoRA server $0.42 |
| eval traffic | measured on G2 wave 1: $0.15 of server time per run at a mean of 4.5 concurrent runs; about $0.03 if 20 slots stay busy | `research/experiments/har129-throughput/` |

Sequence tokens count every token of every row, context included. Trained tokens are the subset that carries loss.
