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
| adapter name differs from base name | 1/5. **Correction (G4):** this was decode nondeterminism, not the adapter. The dry run's one optimizer step had learning rate 0 (warmup), so its LoRA_B stayed 0 and the adapter was the identity. G4's base-vs-base rerun likewise diverges on 1/5 long greedy generations. Whether an adapter is applied is now checked with the logprob probe in §3. |
| `finish_reason` | `stop` on all 15 |

Serving cost about $0.59: LoRA server $0.36, production cold start $0.23.

## 3. G4 training

**Data.** Data's frozen G3 export (HAR-127, 10:48Z):
- conversations sha256 `71a9f7073a0ce4e2a12865fc2a0a74881986ec30067a18bf7e78a528a3875c94`;
- manifest sha256 `567d64a4b78a3775dae89596787851e8e04578a111774932c6d3494af07a522b`;
- 5 captured G2 trials on 5 tasks, 145 per-call rows, all `loss: "last"`.

Dry-run totals on the pinned reader (transformers 5.12.1, tokenizers 0.22.2, jinja2 3.1.6):
`rows=145 tokens=1569955 trained=39913 longest=31403 over_length=0`.

**Config** (frozen by Research-Harbor at 04:53Z; posted on HAR-129 before step 1):
- 1 epoch; LoRA r16, α32, dropout 0.05, on q, k, v, o, gate, up and down;
- lr 5e-5, cosine, 3% warmup; batch 1 × 16 accumulation, so 10 optimizer steps;
- seed 42, set before PEFT init (#648); bf16, gradient checkpointing, no packing;
- `max_length` 65,536, with over-length rows refused.

It ran from main `15d8b708` on app `ap-gccUw9UoMeIUQQF2Kcg3HK`. The receipt is at `g4/receipt.json`.

| | |
|---|---|
| render stack, GPU = dry run | transformers 5.12.1, tokenizers 0.22.2, jinja2 3.1.6 (checked before load) |
| collated label audit | 145/145 rows, 39,913 label tokens = 39,913 mask tokens, 0 mismatched |
| loss by step | 0.162, 0.105, 0.247, 0.222, 0.188, 0.188, 0.127, 0.091, 0.160, 0.151 (train_loss 0.164) |
| learning rate by step | 0 (warmup), 5.0e-5, 4.85e-5 … 1.5e-6 (cosine) |
| real update | LoRA_B max \|value\| 0 → 2.51e-4; LoRA_A init sha256 `0e80c4dd…dc72` |
| train / wall time | 1,330 s / 1,370 s. Step 1 took 558 s (first-step kernel warm-up), and the other 9 averaged about 86 s |
| peak GPU memory | 38.84 GiB (longest row 31,403 tokens) |
| adapter sha256 (`adapter_model.safetensors`) | **`e96209f2a15108e50076e24d7ecaeba43365af055e020f2ab526bb0b270c04a2`**, on volume `evallab-mimo-v26-9b-sft` at `har129-g4/adapter` |
| Modal cost | SFT $1.27 (app `ap-gccUw9…`), LoRA server $0.42 (`ap-Wx0Oc4…`), smoke and probe clients $0.003 |

**Model names:**
- stock: `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`, selector `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`;
- tuned: `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129`, selector `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129`.

Both are served by `serve_lora.py` with `EVALLAB_MIMO_LORA_ADAPTER=har129-g4/adapter EVALLAB_MIMO_LORA_NAME=har129`.

**Serving check.** Both the smoke and the probe ran against the LoRA server only. Production was not redeployed; that saved one cold start.
- **Smoke** (`smoke/raw-g4.json`, `smoke/scored-g4.json`): the same five prompts; the "production" column is a second base-name request. All 15 replies are valid normalized Terminus turns with `stop`. Greedy content: adapter = base on 5/5, base = base rerun on 4/5.
- **Logprob probe** (`tools/modal-mimo-serve/lora_logprob_probe.py`, `smoke/logprob-probe.json`): 64 greedy tokens per prompt, each name requested twice.
  - Base vs base and adapter vs adapter: identical tokens and logprobs, a difference of exactly 0 on 5/5.
  - Base vs adapter: the same tokens, with mean |Δlogprob| 0.004–0.010 and max 0.08–0.17 on 5/5.
  - So the adapter is applied deterministically, and its effect is small.

**Reading.**
- Ten steps over 145 rows from 5 trials at lr 5e-5 move the model measurably but slightly. Greedy text on these prompts is unchanged.
- As Data's freeze note says, G4/G5 exercise the process; they don't test the method.
- The dry-run's 560 tok/s estimate (§1) was itself a first-step measurement. It over-estimated G4's cost about 2× ($2.19 estimated vs $1.27 actual), which is the safe direction for a spend gate, so it stays.

**Chronology.** The G3 samples were selected after G1 v2 (Data's freeze, 10:48Z), and training started at 10:54Z. The dry-run deviation in §1 stands as recorded.

## Reproduce

```bash
uv run python research/experiments/har129-lora/smoke_parity.py prompts prompts.json <trajectory.json>...
EVALLAB_MIMO_LORA_ADAPTER=<run>/adapter EVALLAB_MIMO_LORA_NAME=<name> \
  uv run --project tools/modal-mimo-serve --locked modal deploy tools/modal-mimo-serve/serve_lora.py
uv run --project tools/modal-mimo-serve --locked modal run tools/modal-mimo-serve/lora_smoke.py \
  --prompts prompts.json --lora-url <lora url> --prod-url <prod url> --adapter <name> --out raw.json
uv run python research/experiments/har129-lora/smoke_parity.py score raw.json scored.json
uv run --project tools/modal-mimo-serve --locked modal run tools/modal-mimo-serve/lora_logprob_probe.py \
  --prompts prompts.json --url <lora url> --adapter <name> --out logprob-probe.json
```
