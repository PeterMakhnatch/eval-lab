# tools/modal-mimo-sft

TRL LoRA SFT of `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` (revision `2367e865…ab28177`) on one Modal A100-80GB, trained on an `evallab.sft_terminus` export (HAR-81). `sft.py` is both the CLI and the Modal app; the lab never imports it (subprocess only, like the other `tools/` projects).

## Commands (from the repository root)

```bash
# $0 offline check: verify the export, render every conversation with the
# distill's own template, verify assistant-only masks, print token stats
# and the cost estimate. CPU only; no torch; tokenizer files download once
# from the public HF repo, then the HF cache serves offline runs.
uv run --project tools/modal-mimo-sft --locked \
  python tools/modal-mimo-sft/sft.py dry-run \
  --data tools/modal-mimo-sft/fixtures/tiny-export
# Gated GPU steps: each prints its estimate first and refuses without the flag.
uv run --project tools/modal-mimo-sft --locked \
  python tools/modal-mimo-sft/sft.py train --data <export-dir> --confirm-spend
uv run --project tools/modal-mimo-sft --locked \
  python tools/modal-mimo-sft/sft.py merge --adapter <run>/adapter --confirm-spend
```

`train` uploads the export to the `evallab-mimo-v26-9b-sft` volume, runs `train_remote` (base weights mounted read-only from `evallab-mimo-v26-9b-weights` at the exact `serve.py` layout), and writes `<run>/adapter` + `receipt.json`. `merge` writes merged bf16 weights to `<run>-merged`.

## Cost

`cost = sequence_tokens x epochs / 560 tok/s / 3600 x $2.814912/h`. The cost counts sequence tokens, not trained tokens, because every token of a sample costs a forward and backward pass.

- **Hardware:** A100-80GB + 4 CPU + 16 GiB, the HAR-90 serve shape. The rate is `MIMO_SELFHOSTED_SERVER_USD_PER_HOUR` in `src/evallab/execution_contracts.py`, from modal.com/pricing (2026-09-28).
- **Throughput** was measured in the HAR-129 dry run (2026-10-01):
  - 3 Terminus segments, 143,829 sequence tokens (longest 51,700), one step in 256.9 s;
  - peak GPU memory 52.7 GiB;
  - setup: fla kernels, gradient checkpointing, the chunked selected-token loss.
- **Fixed overhead:** model load and image start are about 60 s, plus the 5-minute idle tail.

The loss is token-mean cross-entropy over trained positions. Only those positions' hidden states reach `lm_head`, in checkpointed chunks of 4,096. The stock path materializes `[sequence, 248320]` logits, about 26 GB in bf16 at 52K tokens, which does not fit next to the model and activations. Before step 1, the trainer runs every row through TRL's own collator and requires the collated labels (`!= -100`) to equal the rendered mask. The counts go into the receipt as `collated_label_check`.

## Pins

**Local lock** (CPU-only, no torch): `modal==1.5.5`, `transformers==5.12.1`, `huggingface_hub==1.9.2`, `jinja2==3.1.6`.

**Modal image** (`TRAIN_IMAGE_PACKAGES` in `sft.py`): `torch==2.14.0`, `transformers==5.12.1`, `trl==1.14.0`, `peft==0.21.0`, `accelerate==1.15.0`, `datasets==5.0.1`, `huggingface_hub==1.9.2`, `flash-linear-attention==0.5.2`, `fla-core==0.5.2`.
- All were verified on PyPI and the set resolves for Python 3.12.
- `transformers==5.12.1` is the version stamped in the model's own `config.json`. It ships `src/transformers/models/qwen3_5/modeling_qwen3_5.py` (tag `v5.12.1`) and already supports `return_assistant_tokens_mask` (verified locally).
- fla supplies the fused gated-delta-rule kernels for the 24 linear-attention layers.

The model loads as the text-only `Qwen3_5ForCausalLM`, and loading fails if any of its weights is missing from the multimodal checkpoint.

## Rendering choices

- **Think blocks.** The template always wraps assistant turns as `<think>{reasoning_content}</think>{content}` (`chat_template.jinja`, pinned revision, assistant macro), the same way for final and non-final turns, with no newlines around the block. Turns without reasoning render `<think></think>`.
  - An assistant message may carry its reasoning in an explicit `reasoning_content` field, exactly as the model emitted it. This is the preferred form; HAR-127 matched it to recorded `completion_tokens` on 217/217 calls.
  - Otherwise a leading `<think>` block in `content` is split off.
  - A message with both forms is rejected.
  - Tool calls stay verbatim `<tool_call><function=...>` text in `content`; the export has no structured `tool_calls` field.
- Training passes `enable_thinking=True`, matching the proxy-enforced serving route (`mimo_selfhosted` in `containers/zai_openapi_secret_proxy.py`). It is the flag the served prompt was rendered with, which matters for the `loss: "last"` prompt render.
- **Masks for `loss: "all"`** (the default) come from the template's `{% generation %}` markers. They are cross-checked against incremental prefix rendering with a verified prefix property, and they include each turn's `<|im_start|>assistant\n` header.
- **Masks for `loss: "last"`** cover exactly the tokens the served model generated for the final call. The served prompt is `messages[:-1]` rendered with `add_generation_prompt=True`, which includes the header. It must be a token prefix of the full render, every token after it must lie inside the template's assistant mask, and only those tokens train: `<think>{r}</think>{m}<|im_end|>`. This is the per-call shape: the history is exactly what the served model saw, with assistant turns carrying no reasoning.
- Any prefix break, mask disagreement or conversation with zero trainable tokens raises an error.
- **Length:** a row longer than `max_length` is refused with its label, never truncated, because a cut row would train on a partial target. The default `max_length` remains 65,536, a training ceiling independent of the configured 262,144-token served context. Longer-sequence training needs a separate memory/cost decision.
- **LoRA defaults** (recorded in the receipt): rank 16, alpha 32, dropout 0.05, lr 5e-5 cosine with 3% warmup, 1 epoch, batch 1 × 16 accumulation, seed 42, bf16, gradient checkpointing, no packing. `--grad-accum` overrides the accumulation.
- **Seed:** `transformers.set_seed(seed)` runs before TRL builds the PEFT model, so the initial LoRA_A is a function of the seed alone. The receipt records its digest.
- **Real update:** training refuses fewer than 2 optimizer steps, because the first warmup step has learning rate 0. The receipt records the learning rates and the LoRA_B max |value|, which starts at 0. The run raises if either never became positive.
- **Render stack:** the GPU image pins transformers 5.12.1, tokenizers 0.22.2 and jinja2 3.1.6, which match this project's lock. `train_remote` refuses to start if the image's stack differs from the one that ran the dry run.

## Adapter vs merge: adapter wins (primary evidence, SGLang v0.5.20)

SGLang serves PEFT LoRA adapters for the `qwen3_5` hybrid: `Qwen3_5ForCausalLM.supported_lora_modules = [qkv_proj, o_proj, out_proj, in_proj_qkvz, gate_up_proj, down_proj, lm_head]` with `packed_modules_mapping` for PEFT names (`python/sglang/srt/models/qwen3_5.py:1555-1570` at tag `v0.5.20`); all are in the LoRA memory pool's known set (`python/sglang/srt/lora/utils.py:396-417`); the linear-attention layer disables its fused GEMM fast path under LoRA so adapters apply (`qwen3_5.py:661-665`). Serve flags (`python/sglang/srt/arg_groups/fields/lora.py:41-76`): `--enable-lora --lora-paths mimo_sft=<adapter-dir>` (rank/targets auto-inferred from `adapter_config.json`), optional `--lora-target-modules q_proj k_proj v_proj o_proj gate_proj up_proj down_proj` (choices in `python/sglang/srt/utils/common.py:4450-4474`). Training targets are restricted to exactly those seven. `merge` remains as the gated fallback: copy the merged dir into the weights-volume layout (`<model-id>/<revision>/`) and point `--model-path` at it.

## Limits

- Never run: image build, 32K-context LoRA memory fit on A100-80GB, or the 1000 tok/s guess are unverified until a gated run. `causal_conv1d`/`fla` kernels are intentionally absent (transformers has torch fallbacks); training works but linear-attention layers run un-fused.
- Dry-run masking is O(turns^2) renders per conversation; fine for pilot scale, not for millions of rows.
- **Reasoning in history.** Training renders every earlier assistant turn with its reasoning. At serving time, SGLang's `mimo` parser moves reasoning into `reasoning_content`, so earlier turns come back to the model with only their `content` unless the harness returns reasoning too. Check which one Terminus-2 sends before reading much into a tuned-vs-base difference. If only `content` comes back, drop reasoning from non-final turns in `to_template_messages`.
- **No route for the adapter yet.** Evaluating an adapter needs the #506 server started with `--enable-lora --lora-paths`, plus a `selfhosted/…` selector that names the adapter. The route currently pins the base model name. Neither is built.
- `train`/`merge` need Modal credentials and `--confirm-spend`; nothing starts without both. Root `pyproject.toml`/`uv.lock` are untouched by design.
