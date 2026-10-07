# tools/modal-mimo-serve

Serves `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` on Modal with SGLang. Eval Lab reaches this backend through the host-side `mimo_selfhosted` proxy, using either Terminus-2 or Xiaomi's native `mimoagent` controller (see `docs/execution-tiers.md`).

## Source configuration

HAR-188 aligns the committed launch configuration to the sourced context and
tool-parser fields in
[`xiaomi-mimo-rl.yaml`](../../research/setup-profiles/xiaomi-mimo-rl.yaml).
This $0 source alignment is not a deployment or live qualification of the
262,144-token window or `qwen3_coder` tool parser.

| Setting | Value |
|---|---|
| App | `evallab-mimo-v26-9b` (Modal Server `MimoServer`) |
| Image | `lmsysorg/sglang:v0.5.20-runtime`, pinned by digest `sha256:00b02004…6f800` (qwen3_5 model + `mimo` reasoning parser; CUDA 13.0) |
| Weights | Modal Volume `evallab-mimo-v26-9b-weights`, HF revision `2367e865d009c13ac81713a2878291d33ab28177` |
| GPU | 1× A100-80GB. `--context-length 262144`, `--reasoning-parser mimo`, `--tool-call-parser qwen3_coder`, `--served-model-name XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` |
| Scaling | `max_containers=1`, `min_containers=0`, `buffer_containers=0`; configurable idle window, default 300 seconds |
| Auth | SGLang `--api-key` from Modal Secret `evallab-mimo-v26-9b-api-key` (`SGLANG_API_KEY`) |

The container's log relay removes the key from SGLang's startup `server_args` line.

HAR-192 adds bounded deployment settings shared with `serve_lora.py`:

| Environment variable | Default | Allowed |
|---|---|---|
| `EVALLAB_MIMO_GPU` | `A100-80GB` | `A100-80GB` or unqualified `L40S`; exactly one GPU |
| `EVALLAB_MIMO_MAX_RUNNING_REQUESTS` | `20` | integer 1–20; SGLang active model requests, not task lanes |
| `EVALLAB_MIMO_SCALEDOWN_WINDOW_SECONDS` | `300` | integer 1–1200 |

Resolved settings are baked into the image so remote imports cannot revert to
defaults. BF16, checkpoint, parsers and 262,144 context remain unchanged; the
shared KV pool still sizes itself at startup. C20 does not promise twenty full
contexts fit or twenty-way throughput. No `min_containers=1` hold is installed:
the pinned Modal 1.5.5 autoscaler update has no TTL. Use one owned campaign
allocation, retain scale-to-zero after failure, and explicitly stop/verify zero
on drain. Idle-window behavior through long tool gaps needs live measurement.

The [HAR-192 design and cost table](../../docs/execution-tiers.md#cheap-campaign-execution-har-192-source-not-deployment)
compare Runpod A100 + unchanged Daytona against Modal A100 + locked Docker on
CCX63. Runpod's 100-second HTTP proxy is unsuitable for the native read timeout;
the design uses authenticated direct TCP/SSH-to-loopback instead. The combined
paid screen is capped at $2 and **has not run**. No serving deployment changed.

The reference profile explicitly marks `server.reasoning_parser` **unsourced**:
the upstream rollout configs name no server reasoning parser. The existing
`mimo` reasoning parser is preserved, not claimed as sourced reference parity.

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

### Native tools: historical acceptance and reference choice

Historical HAR-148 paid acceptance used SGLang 0.5.20's **`mimo` tool-call
parser** with the 65,536-token server. It accepted the distill's
function/parameter XML and returned OpenAI `message.tool_calls`; no Terminus
command translation was used by the native controller. Separate
bash/read/write/edit/agent calls plus plain chat passed with native
`tool_choice=auto`, submitting the matching native schema for each probe.
This did not force a five-schema agent to choose the requested tool. A call
to a function absent from the submitted schema stayed unparsed; a
forced-function grammar probe was not equivalent to automatic tool selection.

The committed **`qwen3_coder` tool-call parser** now follows the reference
profile's sourced training format (`recipes/code/config/train.yaml:59`,
`multi_turn.format: qwen3_coder`). HAR-148's `mimo` acceptance does not qualify
this parser choice. HAR-188's $0 CPU-only replay reconstructed canonical
template text from all 911 HAR-168 tool-bearing outputs (1,096 calls):
both parsers recovered the calls, but 21 outputs (2.3%) differed because
`qwen3_coder` dropped trailing newlines in `write.file_text`. `mimo` also
HTML-unescapes arguments, unlike `qwen3_coder`. There is no raw pre-parser
generated text, so reconstruction cannot prove live equivalence or recover
calls lost by the original parser. HAR-168 recorded zero parse-error alerts;
that absence is not live qualification of `qwen3_coder`.

```bash
# Explicit approval required: five native tools requests plus one plain chat.
keys run -- uv run --project tools/modal-mimo-serve --locked \
  python tools/modal-mimo-serve/smoke.py --tools-only \
  --tool-definitions <native-tools.json> --out <smoke.json>
```

The native controller requires its isolated pinned runtime:
`uv sync --project tools/mimoagent-harbor --locked`. Qualify the server with an
authenticated real `/v1/chat/completions` round trip before paid task dispatch:
a deployed app alone is not readiness, and a bare `/health` pass does not prove
the model path. Every self-hosted dispatch requires a fresh authenticated
readiness result; the campaign qualification waiver does not cache or skip it.
The native retry window covers explicit transient failed transports inside its 300s
known-cold recovery; it is not server-cold qualification and never re-queries
an HTTP 200. The existing Terminus sampling profile stays 0.6/0.95/20; the native
Xiaomi profile is explicitly 1.0/0.95/20.

The host runner binds both the loopback proxy URL and
`EVALLAB_MIMO_SELFHOSTED_PROXY_CAPABILITY` for the native adapter. The
capability authenticates only that trial's proxy; it is not the upstream
provider key and must never enter the task container. A native trace plus
`egress-lock.json` with `applied: true` proves the adapter passed this gate;
successful deployment or zero-spend preflight alone does not.
Native cumulative token backstops do not add or clamp a completion
`max_tokens` setting. Omitted SDK limits stay omitted; new calls reserve the
supervisor's served-context upper bound and are refused when that reservation
does not fit. An explicit SDK limit reserves its original value.

Readiness is time-sensitive: the server scales to zero after five idle
minutes. Re-run the authenticated chat/completions readiness immediately before
dispatch if preparation pauses; an earlier pass is not proof of current
readiness.


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

The HAR-188 context-only increase keeps the same A100-80GB, CPU/RAM allocation
and quoted hourly rate; it does not reserve a full-window KV buffer per request
(see [context sizing](#context-sizing-har-188-source-alignment)).
Longer actual trajectories can increase prefill/generation GPU time, and shared
pool pressure can add queueing or retraction/recompute time. That additional
billed time and its cost are unmeasured: no deployment or paid requests were
made for this alignment. An unchanged rate is not a claim of unchanged
per-trial spend.

## Credential bridge native OTLP adapter (HAR-181)

`credential_bridge.py` adopts the existing credential-relay role into committed
source, backed by `evallab.credential_bridge.create_bridge_app`. It keeps the
same app name, 0.25 CPU / 512 MiB, scale-to-zero (maximum two containers),
readiness behavior and single-forward non-streaming model relay. Historical
paid campaign helpers and their frozen runtime are not modified.

The bridge additionally accepts native OTLP/HTTP `/v1/metrics`, `/v1/traces`
and `/v1/logs`. Metrics become bound observation spans/events in the existing
Laminar trace store; traces/logs retain their native IDs. There is no new
collector backend, durable queue or second store. Inputs support protobuf
and OTLP JSON, identity/gzip encoding and a 2 MiB decompressed limit.
Metric conversion batches source points before export rather than expanding
one ordinary provider batch into an oversized frame.

Configuration is supplied to the **trusted bridge**, not the model/sandbox:

| Setting | Meaning |
|---|---|
| `HAR157_BRIDGE_CAPABILITY` | Existing model-chat/readiness bearer capability |
| `HAR157_MODEL_UPSTREAM` | Existing owned SGLang upstream base URL |
| `evallab-mimo-v26-9b-api-key` | Existing Modal secret containing `SGLANG_API_KEY` |
| `EVALLAB_NATIVE_TELEMETRY_SECRET` | Optional name of a Modal secret containing the next two keys; omission disables native ingestion |
| `EVALLAB_NATIVE_TELEMETRY_CAPABILITY` | Separate native-ingestion bearer capability; must differ from chat capability |
| `LMNR_PROJECT_API_KEY` | Existing Laminar project key, available only to the trusted exporter |
| `EVALLAB_NATIVE_MODEL_SESSIONS` | JSON array of actual shared app/session parent bindings; default `[]` does not guess model attribution |

An admitted provider collector uses this bridge's base URL and the distinct
native bearer capability in its authorization header. It never receives the
Laminar project key or the chat capability. Organization/workspace collector
changes are outside the source-only approval.

Each model binding has `app_id`, integer `started_at_ns`, optional integer
`ended_at_ns`, and `parent`: real SDK `trace_id` (32 hex), `span_id` (16 hex),
`session_id`, optional `model_session`, **no exclusive `trial_id`**. Times and
IDs must come from the actual admitted model session, not examples, billing
windows, reconstructed wall-clock gaps or a trial's phase/root. Native Modal
points must match exactly one app/window; otherwise the OTLP acknowledgment
reports rejected points. App/container/function labels stay in native output.

For Daytona, the Harbor plugin merges actual open trial-root context into
startup `DAYTONA_SANDBOX_OTEL_EXTRA_LABELS`, preserving unrelated labels/env.
The native sandbox `service.instance.id` supplies its UUID. Closed, ambiguous,
unbound and conflicting identities cannot create guessed Laminar roots.
Model metrics stay shared; the converter does not charge them to each trial.

The native exporter uses a separate capability and canonical redaction. It
does not acknowledge success before Laminar accepts all frames; exporter
failure returns non-success and partial rejection counts remain explicit.
There is no added paid-answer retry. Inspect metrics in Laminar **Tree** view,
then **Span Output / Attributes / Events**; DEFAULT observations are omitted
from Transcript and do not become Laminar metric charts.

Provider configuration contracts:
[Daytona organization OTLP](https://www.daytona.io/docs/en/observability/otel-collection.md),
[Modal workspace OTLP](https://modal.com/docs/guide/otel-integration).
Both affect other resources in their organization/workspace. Source and
synthetic protocol/Cloud readback do **not** authorize deployment, changing
those settings or starting compute. Genuine CPU/RAM and native cold/queue/GPU
adoption requires separate current admission. All HAR-168 a3/a4 specs remain
held; native observations do not alter grades, accounting or invoice actuals.
See [observability](../../docs/observability.md#native-provider-metrics-in-laminar-har-181).

## Context sizing (HAR-188 source alignment)

The committed limit is **262,144 tokens**, matching the reference profile's
`recipes/code/config/train.yaml:22` (`max_model_len: 262144`, prompt length
8,192 + response length 253,952) and `recipes/code/run_train.sh:51`
(`MAXLEN=262144`). The pinned checkpoint
[`2367e865d009c13ac81713a2878291d33ab28177` configuration](https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B/raw/2367e865d009c13ac81713a2878291d33ab28177/config.json)
already declares `max_position_embeddings=262144` with default RoPE; no
YaRN extension is needed. This is source/reference evidence, not a new
deployment or 262K live qualification.

The model has eight full-attention layers, four KV heads of dimension 256
and BF16 KV storage: 32 KiB per cached token. One full 262,144-token sequence
needs **8 GiB** of attention KV, plus linear-attention state and runtime
overhead; weights occupy about 17.53 GiB. Source-based sizing supports exposing
the native window on the unchanged A100-80GB. It does **not** support 19
simultaneous full-length sequences: their attention KV alone would need
**152 GiB**, before weights, linear state, activations or other overhead.

[SGLang 0.5.20 allocates a shared token pool](https://github.com/sgl-project/sglang/blob/v0.5.20/python/sglang/srt/mem_cache/memory_pool.py#L259-L287),
not a separate 8 GiB KV reservation for each admitted request. Historical
HAR-116 telemetry implies roughly 745k full-attention token slots
(about 22.75 GiB); this is an inference from the historical
[`server_batches.csv`](../../research/experiments/har129-throughput/data/har116-baseline/server_batches.csv),
not a measured HAR-188 startup pool. Raising the context limit from 65,536
to 262,144 adds only about 0.75 MiB of `req_to_token` index storage per request
row, not 6 GiB of preallocated KV per row. Shorter realistic workloads share
the pool; long resident sequences can cause queueing and retraction/recompute.
No new memory-partition flags or concurrency-policy changes are introduced.
Native sampling remains 1.0/0.95/20, HAR-164 adapters are unchanged, and no
completion clipping is added.

### Historical HAR-148 census (2026-10-02)

The former 65,536-token directive was based on the available HAR-126 census:
9,016 known prompt counts among 9,664 captures, with 648 missing usage records.
There were 327 prompts above 48,000 tokens (3.63% of known prompts), none above
60,000, and a maximum of 57,920. Those were prompt counts, not proof that every
prompt-plus-completion fit or qualification of the 500-step native controller.
The largest observed prompt left 7,616 tokens for its response at 64K.

The historical paired smoke exposed that boundary: SGLang rejected 39,781
input tokens plus a 26,160-token completion allowance (65,941 total) at the
then-unchanged 65,536 limit. The wrapped run and its direct control both stopped
with native `ModelQueryError`, but the direct control hit its 20-request
diagnostic ceiling instead. Their common stop category did not mean identical
model behavior or native-loop qualification. Such terminal stops surface as
structured `infra_error` metadata without tracebacks, not verifier failures.
No completion clipping or context increase was introduced in that historical
smoke; HAR-188 changes only the committed context and tool-parser configuration.


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
