# HAR-104 — MiMo exploration on Terminus-2

Minimal, correct setup for exploration runs of MiMo on Terminus-2. No SFT, no RL.

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
`est_cost_usd` 0.44 = token ceilings at the pinned prices ($0.387) + TTL
sandbox ($0.047 at 1 vCPU / 2 GiB / 10 GiB for agent + verifier + margin).

Treatment-key visibility: the route pins land in `model_revision` (provider
pin + reasoning pin + pinned prices), `serving_image` (`xiaomi/fp8`),
`serving_context_tokens` (1048576), `thinking` (`reasoning_enabled=true`
once every ledger call is shaped), `parser_digest` (`none`), plus the limits
read from `experiment-spec.json`.
