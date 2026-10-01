# tools/modal-mimo-serve

Serves `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` on Modal with SGLang. This is the backend for Eval Lab's Terminus-2 route `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` (proxy provider `mimo_selfhosted`; see `docs/execution-tiers.md`).

## What it runs

| Setting | Value |
|---|---|
| App | `evallab-mimo-v26-9b` (Modal Server `MimoServer`) |
| Image | `lmsysorg/sglang:v0.5.20-runtime`, pinned by digest `sha256:00b02004…6f800` (qwen3_5 model + `mimo` reasoning parser; CUDA 13.0) |
| Weights | Modal Volume `evallab-mimo-v26-9b-weights`, HF revision `2367e865d009c13ac81713a2878291d33ab28177` |
| GPU | 1× A100-80GB. `--context-length 65536`, `--reasoning-parser mimo`, `--served-model-name XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` |
| Scaling | `max_containers=1`, `min_containers=0`, scales to zero after 5 idle minutes |
| Auth | SGLang `--api-key` from Modal Secret `evallab-mimo-v26-9b-api-key` (`SGLANG_API_KEY`) |

The container's log relay removes the key from SGLang's startup `server_args` line.

The Modal proxy is unauthenticated, so any request can wake the container, but every `/v1` call needs the key. `max_containers=1` and the 5-minute idle window bound what an unwanted wake-up can cost. Stop the app when it is not in use.

## Commands (from the repository root)

```bash
# once per weights revision; CPU only
uv run --project tools/modal-mimo-serve --locked modal run tools/modal-mimo-serve/serve.py::download_weights
# deploy (prints the server URL)
uv run --project tools/modal-mimo-serve --locked modal deploy tools/modal-mimo-serve/serve.py
# smoke: cold start, reasoning split, a Terminus-shaped JSON turn, TTFT and decode tok/s at 1 and 8 streams
EVALLAB_MIMO_SELFHOSTED_UPSTREAM=<url> MIMO_SELFHOSTED_API_KEY=<key> \
  uv run --project tools/modal-mimo-serve --locked python tools/modal-mimo-serve/smoke.py --out runs/har90-modal-smoke/smoke.json
# stop (nothing is billed afterwards except volume storage)
uv run --project tools/modal-mimo-serve --locked modal app stop evallab-mimo-v26-9b
```

## Cost

Modal bills per second:

| Resource | Rate |
|---|---|
| A100-80GB | $0.000694/s ($2.4984/h) |
| CPU | $0.0000131 per core-second (4 cores ≈ $0.19/h) |
| Memory | $0.00000222 per GiB-second (16 GiB ≈ $0.13/h) |

Rates are from modal.com/pricing, 2026-09-28. `modal billing report --for today --resolution h --show-resources` gives the measured spend.

A trial's cost is time-based, not token-based:

  server $/h ($2.8149 for GPU, CPU and memory together) × trial hours ÷ concurrent trials + Daytona sandbox time

A cold start and the 5-minute idle tail are billed once per warm period.

## Base + adapter on one server (`serve_lora.py`, HAR-129)

`serve_lora.py` deploys app `evallab-mimo-v26-9b-lora`. It serves the base model and one PEFT LoRA adapter from the SFT volume, `evallab-mimo-v26-9b-sft`, written by `tools/modal-mimo-sft/sft.py train`.

It uses the same image digest, weights, GPU, scaling and secret as production, and the exact `serve.py` launch command (`sglang_command`). It adds only:
- `--enable-lora --lora-paths <name>=/sft/<run>/adapter --max-lora-rank 64 --max-loras-per-batch 1 --lora-strict-loading`;
- the SFT volume, mounted read-only.

Production is a separate app and is never touched.

| Model name in the request | Serves |
|---|---|
| `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` | base weights |
| `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:<name>` | base + adapter (SGLang's `base:adapter` syntax; the response echoes the name) |

```bash
EVALLAB_MIMO_LORA_ADAPTER=<run>/adapter EVALLAB_MIMO_LORA_NAME=<name> \
  uv run --project tools/modal-mimo-serve --locked modal deploy tools/modal-mimo-serve/serve_lora.py
# parity and validity: the LoRA server's two names against production, temperature 0, inside Modal
uv run --project tools/modal-mimo-serve --locked modal run tools/modal-mimo-serve/lora_smoke.py \
  --prompts <prompts.json> --lora-url <lora url> --prod-url <prod url> --adapter <name> --out raw.json
uv run python research/experiments/har129-lora/smoke_parity.py score raw.json scored.json
uv run --project tools/modal-mimo-serve --locked modal app stop -y evallab-mimo-v26-9b-lora
```

With LoRA enabled, SGLang turns off the linear-attention fused GEMM fast path (`qwen3_5.py:661-665` at v0.5.20), and its LoRA kernels run with untuned defaults. Both cost speed, not output. On 2026-10-01 the base name matched production byte for byte on 5 of 5 Terminus prompts at temperature 0 (`research/experiments/har129-lora/`).
