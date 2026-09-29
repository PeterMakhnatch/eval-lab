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
