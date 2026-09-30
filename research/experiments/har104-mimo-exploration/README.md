# HAR-104 — MiMo / gpt-oss-120b exploration on Terminus-2

Minimal, correct setup for exploration runs on Terminus-2. No SFT, no RL.

## What lives here

- `harness/` — the **Harbor-default eval harness tree**. It is the HAR-81 SFT
  tree minus `trajectory_config`: temperature 0.6, `llm_call_kwargs`
  {top_p 0.95, max_tokens 4096}, `proactive_summarization_threshold` 16384.
  Because `trajectory_config` is absent, ATIF defaults apply
  (`raw_content: false`, `linear_history: false`); the treatment key's
  `harness_config_digest` records exactly that.
  Content digest (computed with `evallab.terminus_harness.load_harness_tree`):
  `sha256:433d5d2946e317b0213438ea4aa1852f2aaffa5c3a81a3bbdeb42d4aa028ecf3`.
- `base-specs/terminus2-openrouter-mimo-flash.json` — the exploration base
  spec: `terminus-2` + `openrouter-metered/xiaomi/mimo-v2.6-flash` on
  Daytona, bound to this tree.
- `harness-gpt-oss/` — the same Harbor-default tree with gpt-oss-120b's
  documented sampling instead of MiMo's: temperature 1.0, top_p 1.0;
  max_tokens 4096 and the 16384 summarization threshold unchanged. Digest
  `sha256:b3de3b0da0c315d0395b6bb443e044be1d24879df3d04f6aeeddec4801f961ed`.
- `base-specs/terminus2-openrouter-gpt-oss-120b.json` — the gpt-oss-120b base
  spec: `terminus-2` + `openrouter-metered/openai/gpt-oss-120b` on Daytona,
  bound to `harness-gpt-oss/`.

The HAR-81 tree (`research/experiments/har81-mimo-sft/harness`) is
**SFT-export-only**. No eval run may start from it; the HAR-85 eval base spec
was repointed here accordingly. The HAR-81 `stage.py` `HARNESS` constant is
SFT staging and does not launch eval runs.

## Route: MiMo-V2.6-Flash via OpenRouter

- Selector: `openrouter-metered/xiaomi/mimo-v2.6-flash`. The
  `openrouter-metered/` prefix (not `openrouter/`) keeps litellm's
  `get_llm_provider` on the openai-compatible path through the loopback proxy.
- Upstream pinned to `https://openrouter.ai:443/api/v1/chat/completions`
  (`https_host openrouter.ai`; loopback http only for tests).
- Proxy-enforced request shaping on this provider only: `provider`
  `{"order": ["xiaomi"], "allow_fallbacks": false}` (endpoint tag
  `xiaomi/fp8`; pins upstream serving and price, refuses the fallback pool)
  and `reasoning {"enabled": true}` (MiMo thinking on, matching the
  self-hosted MiMo treatment). Caller-supplied `provider` / `reasoning` /
  `reasoning_effort` are stripped. Ledger calls carry `shaping_applied`.
- Prices pinned at (140 000, 280 000) micros per 1M tokens — $0.14 input /
  $0.28 output (OpenRouter list price verified 2026-09-29; cache read
  $0.0028/M is never credited — the pinned endpoint reports
  `supports_implicit_caching=false`, so uncached input pricing is exact).
- Credential: host env `OPENROUTER_API_KEY`, materialized into an owner-only
  0400 secret file exactly like the other routes. The run fails closed before
  Harbor launches when it is missing.
- Context: 1 048 576 input tokens, at most 131 072 completion tokens,
  runtime-bound in the adapter's `model_info`.
- Parser: the **stock Terminus-2 JSON parser** (Harbor default). This route is
  NOT wrapped in `MimoToolCallParser`; the lead decides from the proof run's
  raw outputs whether the normalizer is needed. Raw model text is preserved.

## Limits (per trial)

`max_requests` 120 · `max_input_tokens` 2 500 000 · `max_output_tokens` 131 072 ·
`max_total_tokens` 2 631 072 · `cost_limit_usd` 0.50 · `timeout_seconds` 900
(the task's own `task.toml` agent timeout — candidate-0036 declares 900).
`est_cost_usd` 0.55 = the $0.50 cost cap (`tasks prepare` requires the
estimate to cover it; the token ceilings at the pinned prices are $0.387) +
TTL sandbox ($0.047 at 1 vCPU / 2 GiB / 10 GiB for agent + verifier + margin).

gpt-oss-120b arm: the same request and token limits; `cost_limit_usd` 0.12
covers its token ceiling at the pinned prices (2.5M × $0.037 + 131 072 ×
$0.17 = $0.115), so it never binds first; `est_cost_usd` 0.17 = 0.12 + the
same sandbox TTL.

Treatment-key visibility: the route pins land in `model_revision` (provider
pin + reasoning pin + pinned prices), `serving_image` (`xiaomi/fp8`),
`serving_context_tokens` (1048576), `thinking` (`reasoning_enabled=true`
once every ledger call is shaped), `parser_digest` (`none`), plus the limits
read from `experiment-spec.json`.

## Route: gpt-oss-120b via OpenRouter

- Selector: `openrouter-metered/openai/gpt-oss-120b`, same proxy, key and
  upstream pin as MiMo; a trial's proxy admits only its own model.
- Endpoint `deepinfra/bf16`: `provider {"order": ["deepinfra/bf16"],
  "allow_fallbacks": false}`. The full slug matters — bare `deepinfra` also
  matches DeepInfra's turbo (16K output cap) and fp8 endpoints at 4–5× the
  price. Chosen as the cheapest high-uptime endpoint with the full 117 964
  completion cap (CoreWeave fp4 is $0.007/M cheaper on input).
- `reasoning {"effort": "medium"}` (the model's default), prices $0.037 /
  $0.17 per 1M (verified 2026-09-30, `supports_implicit_caching=false`),
  context 131 072 with at most 117 964 completion tokens.
- Treatment key: `serving_image` `deepinfra/bf16`, `serving_context_tokens`
  131072, `thinking` `reasoning_effort=medium`.
