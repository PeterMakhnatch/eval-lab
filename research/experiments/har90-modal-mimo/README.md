# HAR-90: MiMo-V2.6 9B on Modal behind a Terminus-2 route

This directory holds the evidence that the route works end to end. Peter authorised it through HAR-90 with a $5 total cap.

The route is the Terminus-2 selector `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`, served through the loopback proxy provider `mimo_selfhosted`. See `docs/execution-tiers.md`.

The server is `tools/modal-mimo-serve/` (see its README).

## Server

| Item | Value |
|---|---|
| Modal app | `evallab-mimo-v26-9b`, class `MimoServer` (`@app.server`) |
| URL | `https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct` |
| Image | `lmsysorg/sglang:v0.5.20-runtime` @ `sha256:00b02004501e402332827ffd5343a225a8960d99adc990b37d6f31085b8f6800` |
| Weights volume | `evallab-mimo-v26-9b-weights`; HF revision `2367e865d009c13ac81713a2878291d33ab28177` (4 safetensors shards) |
| API-key secret | `evallab-mimo-v26-9b-api-key` (`SGLANG_API_KEY`) |
| GPU | 1× A100-80GB, `max_containers=1`, scales to zero after 300 s |

## Smoke (`smoke.json`, 2026-09-29)

- **Cold start:** 208 s from the first request to `/health` returning 200. Modal answered 503 40 times meanwhile. This covers the container start, loading weights from the volume, and CUDA-graph capture.
- **Reasoning split:** `reasoning_content` came back separate from `content` (`split_ok: true`, no `<think>` in `content`).
- **Terminus-shaped turn:** returned a JSON object with the keys `analysis`, `plan`, `commands`, `task_complete`.

| Load | TTFT | Decode | Wall |
|---|---|---|---|
| Concurrency 1, 19-token prompt, 512 tokens, ×3 | 0.23 s | 84.5 tok/s | 6.3 s |
| Concurrency 1, 13,824-token prompt, 512 tokens | 1.50 s | 82.1 tok/s | 7.7 s |
| Concurrency 8, 512 tokens each | median 0.44 s | median 80.7 tok/s per stream; 603.6 tok/s aggregate | 6.8 s |

## Trials

Both trials ran against a warm server, so neither includes a cold start.

| | Setting / result | Trial 1 | Trial 2 |
|---|---|---|---|
| Setup | Task | `candidate-0036-software-data-engineering` | same task |
| | Split / harness | HAR-81 train split, not held-out; HAR-81 `harness-student` tree (`raw_content`, `linear_history`) | same |
| | Spec | `01M3NABR7H5SPBKE0K6J5E34QM` | `01M3NB2PRGZBTNTJ8120C545G1` |
| | Evidence | `runs/har90-mimo-0036/har90-mimo-0036__mWHLdCT` | `runs/har90-mimo-0036-b/har90-mimo-0036-b__XfPjwNk` |
| | Ceilings | 200 requests / 2.5M input tokens | 200 requests / 16M input tokens |
| Timing | Trial wall time | 631 s (agent 590 s) | 447 s (agent 423 s) |
| | Requests | 120 | 200 (hit the request ceiling) |
| | Tokens in / out | 2,389,120 / 13,478 | 2,404,842 / 8,704 |
| | End | Hit the input-token ceiling ("trial budget exhausted") | Hit the request ceiling |
| Outcome | Reward | None: the verifier never ran | None: the verifier never ran |
| | Queue state | `failed`, `model_identity_mismatch` (a route bug, fixed below) | `done`; identity matched |
| Trajectory | Turns that parse as Terminus JSON | 119 of 120 | 15 of 200 |
| | Turns with non-empty `reasoning_content` | 3 | 10 |
| | `<think>` inside `content` | 0 | 0 |
| | Proxy ledger | 120 calls, all `shaping_applied`, $0 | 200 calls, all `shaping_applied`, $0 |

Notes on the trials:

- **Reward:** in both trials the ceiling fired as a LiteLLM `RateLimitError`, and Harbor does not run the verifier after an agent exception. The reward is therefore missing, not 0.
- **Identity fix:** trial 1 was classed `model_identity_mismatch` because SGLang echoes its served name, `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`. The runner now accepts that name for this selector (`_accepted_returned_models`). Trial 2 verified the fix live.
- **Secrets:** no provider key or token value appears anywhere in either run, the queue records or this directory.

### Finding: the model's native tool-call format collides with Terminus JSON

MiMo-V2.6-Distill-Qwen-9B keeps emitting its RL-harness tool-call wrapper, `<tool_call><function=exec_command>{"keystrokes": …}</function></tool_call>`.

- **Trial 1:** the wrapper prefixed a valid Terminus object on 119 of 120 turns. Terminus warned "Extra text detected before JSON object" and still parsed it.
- **Trial 2:** from turn 2 onward the model emitted only native tool calls. It then looped: 170 of 200 turns repeated the same `pwd` call after Terminus's parse-error feedback.
- **In both trials:** keystrokes often lacked the trailing newline, so commands ran together on one line.

The route itself works: the reasoning stays separate, the identity matches, the ceilings hold and nothing leaks. But this model does not follow the Terminus-2 JSON protocol reliably. A harness with native tool calling fits its prior better.

## Cost per trial (for HAR-81 staging)

Per-token spend on this route is $0; the proxy ledger pins `(0, 0)`. Modal bills the server container per second, and the sandbox is billed separately:

  trial_usd = 2.8149 × trial_hours ÷ concurrent_trials + 0.0834 × sandbox_hours

- **Server, $2.8149/h:** A100-80GB $2.4984 + 4 cores $0.1886 + 16 GiB $0.1279 (modal.com/pricing, 2026-09-28). `mimo_selfhosted_trial_cost_usd` implements this.
- **Daytona, $0.0834/h:** 1 vCPU and 2 GiB ($0.0504/vCPU-h + $0.0162/GiB-h × 2).
- **Each warm period** also costs a cold start plus the idle tail, (208 s + 300 s) × $2.8149/h ≈ $0.40.
- **By the formula:** trial 1 = $0.494 server + $0.015 sandbox; trial 2 = $0.350 + $0.010.
- **Concurrency:** `max_containers=1`, and SGLang batches concurrent trials on one GPU. At concurrency 8 each stream still decoded at about 81 tok/s, so the server share per trial falls roughly as 1 ÷ concurrency.
