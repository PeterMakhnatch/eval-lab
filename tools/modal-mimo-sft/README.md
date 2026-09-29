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

`cost = trained_tokens x epochs / 1000 tok/s (ASSUMED) / 3600 x $2.814912/h` (A100-80GB + 4 CPU + 16 GiB, the HAR-90 serve shape; `MIMO_SELFHOSTED_SERVER_USD_PER_HOUR` in `src/evallab/execution_contracts.py`, rates from modal.com/pricing 2026-09-28). Time only, plus the 5-minute idle tail per warm period. The throughput is a labeled guess recorded in every receipt, not a measurement.

## Pins

Local lock (CPU-only, no torch): `modal==1.5.5`, `transformers==5.12.1`, `huggingface_hub==1.9.2`, `jinja2==3.1.6`. Modal image (`TRAIN_IMAGE_PACKAGES` in `sft.py`): `torch==2.14.0`, `transformers==5.12.1`, `trl==1.14.0`, `peft==0.21.0`, `accelerate==1.15.0`, `datasets==5.0.1`, `huggingface_hub==1.9.2` (all verified on PyPI 2026-09-29; the set resolves for Python 3.12). `transformers==5.12.1` is the version stamped in the model's own `config.json` and ships `src/transformers/models/qwen3_5/modeling_qwen3_5.py` (tag `v5.12.1`); it also already supports `return_assistant_tokens_mask` (verified locally).

## Rendering choices

- The template always wraps assistant turns as `<think>{reasoning_content}</think>{content}` (`chat_template.jinja`, pinned revision, assistant macro), identically for final and non-final turns — so the export's `<think>` prefix maps to `reasoning_content` on every assistant turn, and turns without it render `<think></think>`. Tool calls stay verbatim `<tool_call><function=...>` text in `content` (the export has no structured `tool_calls` field).
- Training passes `enable_thinking=True`, matching the proxy-enforced serving route (`mimo_selfhosted` in `containers/zai_openapi_secret_proxy.py`). With no trailing generation prompt it changes no training token; it is passed for consistency and recorded.
- Masks come from the template's `{% generation %}` markers, cross-checked against incremental prefix rendering with a verified prefix property (both agree on the fixture). Any prefix break, mask disagreement, or zero-trainable-token conversation raises loudly. Truncation mirrors TRL: right-truncate at `max_length` (default 32768, `keep_start`), drop fully-masked rows. LoRA defaults (recorded in the receipt): rank 16, alpha 32, dropout 0.05, lr 1e-4 cosine, 1 epoch, batch 1 x 16 accum, bf16, grad checkpointing, no packing.

## Adapter vs merge: adapter wins (primary evidence, SGLang v0.5.20)

SGLang serves PEFT LoRA adapters for the `qwen3_5` hybrid: `Qwen3_5ForCausalLM.supported_lora_modules = [qkv_proj, o_proj, out_proj, in_proj_qkvz, gate_up_proj, down_proj, lm_head]` with `packed_modules_mapping` for PEFT names (`python/sglang/srt/models/qwen3_5.py:1555-1570` at tag `v0.5.20`); all are in the LoRA memory pool's known set (`python/sglang/srt/lora/utils.py:396-417`); the linear-attention layer disables its fused GEMM fast path under LoRA so adapters apply (`qwen3_5.py:661-665`). Serve flags (`python/sglang/srt/arg_groups/fields/lora.py:41-76`): `--enable-lora --lora-paths mimo_sft=<adapter-dir>` (rank/targets auto-inferred from `adapter_config.json`), optional `--lora-target-modules q_proj k_proj v_proj o_proj gate_proj up_proj down_proj` (choices in `python/sglang/srt/utils/common.py:4450-4474`). Training targets are restricted to exactly those seven. `merge` remains as the gated fallback: copy the merged dir into the weights-volume layout (`<model-id>/<revision>/`) and point `--model-path` at it.

## Limits

- Never run: image build, 32K-context LoRA memory fit on A100-80GB, or the 1000 tok/s guess are unverified until a gated run. `causal_conv1d`/`fla` kernels are intentionally absent (transformers has torch fallbacks); training works but linear-attention layers run un-fused.
- Dry-run masking is O(turns^2) renders per conversation; fine for pilot scale, not for millions of rows.
- `train`/`merge` need Modal credentials and `--confirm-spend`; nothing starts without both. Root `pyproject.toml`/`uv.lock` are untouched by design.
