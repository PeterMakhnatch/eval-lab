# HAR-129: LoRA SFT on Modal and a base + adapter server with parity

G4 of the 2026-10-01 overnight plan
(`~/Developer/research-context/inbox/sft-overnight-20260918/OVERNIGHT-2026-10-01.md`).
The stock and tuned arms of the paired eval (G5) are served by one SGLang server under two model
names. The tuned arm is a LoRA adapter trained on verified passes.

Tools:
- `tools/modal-mimo-sft/sft.py` trains.
- `tools/modal-mimo-serve/serve_lora.py` serves.
- `tools/modal-mimo-serve/lora_smoke.py` sends the smoke requests.
- `smoke_parity.py` (here) builds the prompts and scores the replies.

Operator steps (train, serve, parity, cost): [`docs/lora-sft-runbook.md`](../../../docs/lora-sft-runbook.md).

## 1. Dry run (2026-10-01 04:10–04:20Z)

**Data.** The card's three existing counted passes:
- HAR-104 `002391` and `002864`, plus HAR-116 `000495-loopfix-r2`. All are among HAR-116's 10 tasks, which the G1 eval set excludes. They were selected at 04:08Z.
- The sft_terminus exporter needs the original trial roots. These passes have only the ATIF trajectory, so `dryrun/build_dry_export.py` approximates each conversation from the recorded step layers: the raw proposed message, its reasoning, then the observation.
- This export is for measuring plumbing, time and memory only; it is not a training set.
- **Chronology deviation (Cdx 3, HAR-133).** These samples were selected at 04:08Z, before G1's first commit `f8924596` (04:09:33Z). The plan required the eval set to be frozen before any training data was chosen, so this run does not comply with that ordering. It is recorded here as a deviation. No claim of compliance is made for it. The three tasks are outside the G1 v2 eval set (sha `3b997fdc…`), and this adapter was deleted.

**Run.** One optimizer step over all 3 rows on one A100-80GB, with `--grad-accum 1 --epochs 1`. The receipt is at `dryrun/receipt.json`.

| | |
|---|---|
| sequence tokens / trained tokens | 143,829 / 40,111 (longest row 51,700) |
| truncated / dropped at `max_length` 65,536 | 0 / 0 |
| step time | 256.9 s, so about **560 sequence tokens/s** |
| wall time (image start, model load, render, train, save) | 318.8 s |
| peak GPU memory | 52.66 GiB |
| loss | 0.3314 |
| adapter sha256 | `5be702d0162ffb7206ce7362affd91b47bf39d036da1e6df3ff436a119a07724`, **deleted after the smoke** |
| Modal cost | $0.28 (app `evallab-mimo-v26-9b-sft`, billing report) |

**What had to change in `sft.py` before it ran:**
1. **The stock loss does not fit.** TRL computes `[sequence, 248320]` logits: about 26 GB in bf16 at 52K tokens, before the fp32 upcast. `SelectedTokenLossTrainer` sends only the hidden states of trained positions through `lm_head`, in checkpointed chunks of 4,096. It returns the same token-mean cross-entropy, normalized by the accumulated token count.
2. **fla 0.5.2** supplies fused kernels for the 24 linear-attention layers. Without them, transformers falls back to a torch loop.
3. **Fail-loud weight loading.** The text-only class loads from the multimodal checkpoint and raises if any weight is missing.
4. **`--grad-accum` was ignored** (the default 16 was always used); it is now honoured and recorded.
5. **Plain-JSON receipt.** `log_history` could carry tensors that the CPU-only client cannot unpickle, so the receipt is now plain JSON.
6. **Collated-label audit before step 1.** The rows TRL prepared must match the rendered rows: the same count and order, identical `input_ids`, and collated labels (`!= -100`) equal to the rendered mask. Any mismatch stops the run, and the counts go in the receipt. TRL 1.14 drops `assistant_masks` after folding them into labels, so the audit compares against the rendered mask instead (PR #617). `cpu_trl_proof.py` checks this on the pinned stack at $0, on the local MPS/CPU device in fp32.
7. **Loss on the last turn only.** A per-row `"loss": "last"` keeps only the final assistant turn, for per-call samples (Data's option A on HAR-127).
8. **Cost estimate** uses the measured rate per sequence token (about $1.40 per 1M) instead of a guessed 1,000 trained tokens/s.

## 2. Serving and parity

`serve_lora.py` uses `serve.py`'s image digest and launch command unchanged. It differs from production only as follows:
- LoRA flags: `--enable-lora --lora-paths <name>=<dir> --max-lora-rank 64 --max-loras-per-batch 1 --lora-strict-loading`;
- the SFT volume is mounted read-only;
- it runs as its own app;
- with LoRA enabled, SGLang turns off the linear-attention fused GEMM path, and its LoRA kernels use untuned defaults. Both affect speed only.

SGLang loaded the adapter into `qkv_proj, o_proj, gate_up_proj, down_proj`.

**Smoke (04:22–04:29Z).**
- **Prompts:** the exact first Terminus-2 prompt of five HAR-116 loopfix trials (`smoke/prompts.json`).
- **Arms:** each prompt went to the LoRA server's base name, its adapter name (dry-run adapter `dryrun1`) and production `evallab-mimo-v26-9b`, redeployed unchanged from `origin/main` and stopped afterwards.
- **Settings:** temperature 0, `enable_thinking`, `max_tokens` 4096. The requests ran inside Modal, so the API key never left it.
- **Files:** `smoke/raw-dryrun1.json` holds the replies; `smoke/scored-dryrun1.json` holds the scores.

| | result |
|---|---|
| valid Terminus-2 turn, all 3 names | **15/15**: native `<tool_call>` text that the lf2 normalizer (`evallab.mimo_tool_calls`) turns into a command, the same format production emits. 0/15 were strict JSON, which no arm emits. |
| LoRA server base name vs production, content and reasoning | **5/5 byte-identical** |
| adapter name differs from base name | 1/5 (after a single training step), so the adapter is applied |
| `finish_reason` | `stop` on all 15 |

Serving cost about $0.59: LoRA server $0.36, production cold start $0.23.

## 3. G4 training

Pending G3's frozen export (HAR-127). It will be recorded here: export sha256, adapter sha256, loss curve, cost, and both model names.

## Reproduce

```bash
uv run python research/experiments/har129-lora/smoke_parity.py prompts prompts.json <trajectory.json>...
EVALLAB_MIMO_LORA_ADAPTER=<run>/adapter EVALLAB_MIMO_LORA_NAME=<name> \
  uv run --project tools/modal-mimo-serve --locked modal deploy tools/modal-mimo-serve/serve_lora.py
uv run --project tools/modal-mimo-serve --locked modal run tools/modal-mimo-serve/lora_smoke.py \
  --prompts prompts.json --lora-url <lora url> --prod-url <prod url> --adapter <name> --out raw.json
uv run python research/experiments/har129-lora/smoke_parity.py score raw.json scored.json
```
