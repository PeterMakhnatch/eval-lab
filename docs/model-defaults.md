---
status: living
audience:
  - operator
  - runner
---

# Which model for which job

Default model choices for new specs. Rule: the cheapest model that is good
enough for the job, **subscription first**. Wherever one of Peter's
subscriptions offers a suitable model, the job runs there; the OpenRouter
pay-per-token routes are the fallback for open models no subscription offers
(Qwen, GLM, DeepSeek, MiMo as the agent under test). When a subscription
rate-limits, back off and wait for the window; do not switch that job to an
API.

Numbers are per 1,000 trajectories, from token counts we recorded. For
subscription rows the figure is the API list-price equivalent: it is not
billed, but it shows how much of the shared quota the job draws relative to
the alternatives.

## Subscriptions

| Subscription | Reached through | Models |
|---|---|---|
| Claude Max | Claude Code (Harbor `claude-code` adapter, `scripts/with-claude-auth`); the OMP auth-gateway for judge calls (fxtr `omp` route) | Haiku 5.5, Sonnet 5.5, Opus 5.5 |
| ChatGPT (`prolite` plan) | `codex exec` (Harbor `codex` adapter, `~/.codex/auth.json`) | gpt-6.1-sol, gpt-6-sol, gpt-6-astra, gpt-6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-reserve |

**Codex CLI version matters.** The model list above is what codex CLI
0.162.1 offers on this subscription (2026-10-12; `gpt-6.1-sol` verified
answering through `codex exec`). Older CLIs reject the gpt-6 models: 0.154.0
returned HTTP 400 for `gpt-6-luna`. Two pins in this repo still name old
versions and must be raised before a gpt-6 model can run there:
`PINNED_CODEX_VERSION = "0.148.0"` in `src/evallab/harbor_codex.py` (the CLI
Harbor installs inside task sandboxes) and `CODEX_VERSION = "codex-cli
0.154.0"` in `src/evallab/gepa_optimizer/codex_transport.py` (the host CLI the
GEPA proposer requires exactly; with the host now on 0.162.1 that transport
refuses to start until the pin moves).

Limits we have hit or must respect:

- **Codex reached 92% of its 7-day window** (2026-08-16, plan `prolite`, no
  overflow credits). At 100% every Codex trial is locked out until the window
  resets; it does not bill. The window is account-wide (Peter's interactive
  use counts) and the counter moves in whole percentage points, so the lab's
  share cannot be measured. `docs/quota-accounting.md` has the evidence and
  the gate (`evallab.quota`), which refuses billable work only when the
  provider reports the window exhausted.
- **Claude Max:** no limit hit recorded. The fxtr judge projects ran about
  8M input tokens through the OMP gateway without a rate limit. Recorded
  failure modes are content filters, not quota: Sonnet 5.5 refuses
  transcripts that include the agent's reasoning (`reasoning_extraction`), and
  the gateway ignores `response_format`, so judges unwrap fenced JSON.
- Back-off rule for both: on a rate-limit response, stop launching, record
  the reset time, resume after it. Keep concurrency at 8 or below on the OMP
  gateway.

## Defaults by job

| Job | Default (subscription) | Fallback (open models only) | Evidence | $ per 1k |
|---|---|---|---|---|
| Bulk judge (first pass over every transcript) | Claude Haiku 5.5 via the OMP gateway | none for rate limits (back off); GLM-5.3-Flash only where Claude is unsuitable (e.g. repeated refusals), via the CheatBench judge's Z.ai route (balance empty since 2026-10-09) | Haiku 5.5 agrees with Sonnet on 82% of CheatBench trials, catches 37/40 labelled hacks; 8% unparseable replies need the adjudicator | $4.40 equivalent (CheatBench prompt), $1.21 (scorecard prompt); GLM $6.40 billed |
| Adjudication judge (Haiku-clean, unparseable, disputed) | Claude Sonnet 5.5 via the OMP gateway | same rule; GLM-5.3 where Claude is unsuitable | Sonnet 5.5: 48/49 on CheatBench, agrees with the detector and every stored judge | $88 equivalent per 1k adjudicated; GLM-5.3 $60 billed |
| Cheap cheater (prompted to find and use holes) | Codex `gpt-5.6-luna` via `codex exec` (gpt-5.6-terra has run on the Harbor CLI pin; luna on that pin is untested; `gpt-6-luna` needs the pins raised); second choice Claude Code with Haiku 5.5 | GLM-5.3-Flash (`openrouter-metered/z-ai/glm-5.3-flash`), proven in cheater-recall wave 1 and every CheatBench family | subscription cheaters are unproven: run a 10-task pilot before a campaign | gpt-5.6-luna ≈ $11 equivalent (Codex trial shape); GLM fallback $31 billed |
| Agent under test, a frontier model | that model's own CLI on its subscription: Codex (`gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`; gpt-6 models need the CLI pins raised) or Claude Code (Sonnet/Opus 5.5) | none (frontier models are not run on OpenRouter while a subscription offers them) | Codex canaries: 146K input, 91% cached, 4.5K output per trial | gpt-5.6-terra ≈ $108 equivalent |
| Agent under test, an open model | no subscription offers these | OpenRouter pinned routes: GLM-5.3-Flash, DeepSeek V4.1-Flash, MiMo-V2.6-Flash, gpt-oss-120b | see the route table below | $11–53 billed |
| Agent under test, the student | MiMo-V2.6-Distill-Qwen-9B (+ adapters) on our Modal server, `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` | none | only route serving those weights | $30–150 of GPU time (20 vs 4.5 trials at once) |

## OpenRouter routes for open models

Pinned first-party endpoints, list prices read from OpenRouter's endpoint API
on 2026-10-12. Per 1k trajectories for two recorded shapes.

| Model | Selector | Cheater attempt | Long repo task | Note |
|---|---|---|---|---|
| GLM-5.3-Flash, Z.ai | `openrouter-metered/z-ai/glm-5.3-flash` | $31 | $53 | default open cheater; proven |
| DeepSeek V4.1-Flash, DeepSeek | `openrouter-metered/deepseek/deepseek-v4.1-flash` | $17 (peak $35) | $27 (peak $55) | prompt caching confirmed on this endpoint; DeepSeek peak hours (Mon–Fri 01–04 and 06–10 UTC) cost double |
| MiMo-V2.6-Flash, Xiaomi | `openrouter-metered/xiaomi/mimo-v2.6-flash` | $11 if cache hits are billed, $89 if not | $22 / $158 | endpoint uptime ~90% on 2026-10-12 |
| gpt-oss-120b, DeepInfra | `openrouter-metered/openai/gpt-oss-120b` | $25 | $43 | no cache pricing; 131K context per call |

Not for agent runs: Tinker Qwen3.6-35B-A3B costs $90–156 per 1k with cached
prefill credited and is metered without that credit ($293–512). Keep Tinker
for training.

## Trajectory shapes behind the numbers

| Shape | Input per trajectory | Cached share | Output | Source |
|---|---|---|---|---|
| Cheater attempt (terminus-2) | 0.60M | 94% | 17K | cheater-recall-v1 samples (GLM-5.3-Flash 516K/93%, Qwen3.6 658K/95%) |
| Long repo task (terminus-2) | 1.10M | 90% assumed | 13K | G5 sample `ovn-g5-001626-tuned` (self-hosted; no cache reported) |
| Codex trial | 146K | 91% | 4.5K | mean of 17 Codex canaries, `docs/quota-accounting.md` |
| CheatBench judge call | 40K | none | 0.8K | fxtr `cheatbench-judges`, mean over 49 |
| Scorecard reader call | 9.3K | none | 0.56K | fxtr `detector-scorecard`, mean over 40 |

Agent cost is almost all cached input, so cached-input prices decide the
ranking among billed routes.

## The other half of the bill: sandboxes

A Daytona sandbox costs about $0.074 per trajectory at G5 lifetimes ($74 per
1k), more than any billed open model above. Local Docker is free but
unreliable under load. Size a campaign's sandbox budget first.
