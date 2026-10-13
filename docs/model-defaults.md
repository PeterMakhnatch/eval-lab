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
returned HTTP 400 for `gpt-6-luna`. Both pins in this repo are now 0.162.1:
`PINNED_CODEX_VERSION` in `src/evallab/harbor_codex.py` (the CLI Harbor
installs inside task sandboxes; base and linux-arm64 optional packages both
published) and `CODEX_VERSION` in
`src/evallab/gepa_optimizer/codex_transport.py` (the host CLI the GEPA
proposer requires exactly).

Limits we have hit or must respect:

- **Codex reached 92% of its 7-day window** (2026-08-16, plan `prolite`, no
  overflow credits). At 100% every Codex trial is locked out until the window
  resets; it does not bill. The window is account-wide (Peter's interactive
  use counts) and the counter moves in whole percentage points, so the lab's
  share cannot be measured. `docs/quota-accounting.md` has the evidence and
  the gate (`evallab.quota`), which refuses billable work when the provider
  reports the window exhausted — and since 2026-10-13 also at the lab's own
  ceiling of 60% used (`subscription_quota_ceiling`).
- **Claude Max:** no limit hit recorded. The fxtr judge projects ran about
  8M input tokens through the OMP gateway without a rate limit. Recorded
  failure modes are content filters, not quota: Sonnet 5.5 refuses
  transcripts that include the agent's reasoning (`reasoning_extraction`), and
  the gateway ignores `response_format`, so judges unwrap fenced JSON.
- Back-off rule for both: on a rate-limit response, stop launching, record
  the reset time, resume after it. Keep concurrency at 8 or below on the OMP
  gateway.

## Subscription agents in Eval Lab

How each subscription agent runs inside task sandboxes, with the exact
working invocation and the trial that proved it. (One subsection per agent;
each lane owns its own.)

### Codex

Harbor adapter `evallab.harbor_codex:PinnedCodex` (`PINNED_CODEX_VERSION =
"0.162.1"`); host GEPA proposer requires `codex-cli 0.162.1` exactly
(`CODEX_VERSION` in `src/evallab/gepa_optimizer/codex_transport.py`). Auth is
the ChatGPT subscription in `~/.codex/auth.json` — never an API key.

Trial invocation (local Docker, per-spec authorisation):

```bash
uv run evallab tasks prepare library/tasks/event-summary \
  --name codex-gpt-6-luna-event-summary --agent codex --model gpt-6-luna
uv run evallab submit derived/prepared/codex-gpt-6-luna-event-summary.json
uv run evallab approve <spec-id> --actor peter
uv run evallab tick --spec-id <spec-id>
```

Proof trial 2026-10-13 (`runs/codex-gpt-6-luna-event-summary/`, job dir
`codex-gpt-6-luna-event-summary__PffhRBE`, task `local-lab/event-summary`):
model `gpt-6-luna` on sandbox CLI 0.162.1, reward **1.0** (correctness /
input_preservation / output_hygiene all 1.0), 139s wall. Token usage
(`result.json` `agent_result`): 27,774 input (24,064 cached), 392 output,
$0.0008 API list-price equivalent — not billed. Quota snapshot from the
trial's own rollout (`agent/sessions/2026/10/13/rollout-*.jsonl`,
`token_count` → `rate_limits`): window **1.0% used** at 05:48 UTC
(`window_minutes` 10080, `resets_at` 2026-10-20T05:39:30Z, plan `pro`,
credits balance 61259.43, `hard_stop` false). Before the batch the freshest
local snapshot (2026-10-12 host session) read 21% in the previous window —
both far below the 60% ceiling, so dispatch was never gated.

GEPA proposer proof the same night: `CodexTransport(...,
model="codex/gpt-6-astra").preflight()` reports `codex-cli 0.162.1` on the
`chatgpt_subscription` route, and one single-turn `request()` completed
(13,489 input / 4,096 cached / 5 output) — the `exec --json` event shapes
parse unchanged.

### Claude Code

Status: **proven 2026-10-13** through the gateway lane below (reward 1.0 on
`event-summary`). The host `claude` login is expired and the Keychain item
is absent, so the Keychain lane stays an unproven alternative.

Invocation (local Docker, per-spec authorisation; the proving-trial commands):

```bash
uv run evallab tasks prepare library/tasks/event-summary \
  --name claude-gateway-haiku-event-summary --agent claude-code \
  --model claude-gateway/claude-haiku-5-5
uv run evallab submit derived/prepared/claude-gateway-haiku-event-summary.json
uv run evallab approve <spec-id> --actor peter
uv run evallab tick --spec-id <spec-id>
```

Transport: the lab-owned host-loopback bridge
(`containers/claude_gateway_bridge.py`) fronts one `omp auth-gateway stdio`
child per job and answers the sandbox's `POST /v1/messages` from OMP's local
Claude login. The sandbox reaches it as `host.docker.internal` and presents
a per-trial capability as `ANTHROPIC_AUTH_TOKEN` (custom-base-URL auth);
`CLAUDE_FORCE_OAUTH` stays off or Harbor refuses before any sandbox starts.
The sandbox never sees a provider credential — no gateway bearer exists in
this mode — and the capability redacts from Harbor logs and scans from
persisted job trees exactly like the proxy-held keys. The bridge checks the
served model against the requested one on every call (prefix-tolerant for
the gateway's `anthropic/` echo, fail-closed 502 otherwise), caps gateway
concurrency at 4, and forwards 429s verbatim so the queue backs off instead
of retrying into the limit. The runner persists the bridge usage ledger
beside the job (`claude-gateway-usage.jsonl`: per-call models, statuses,
token counts — never the capability).

Models verified: `claude-gateway/claude-haiku-5-5` (cheapest; Harbor receives
`anthropic/claude-haiku-5-5` and keeps it whole for the custom base URL).
Other pinned profiles: `claude-code-fable-5` (adapter default, Keychain lane)
and `claude-code-haiku-5-5` (Keychain lane). The sandbox installs the CLI per
trial, so each trial pays minutes of `agent_setup`; the task needs
`network_mode = "public"` (the `event-summary` default) for the install and
the bridge calls.

Proof trial 2026-10-13 (`runs/claude-gateway-haiku-event-summary/`, trial
`claude-gateway-haiku-event-summa__v3926MH`, task `local-lab/event-summary`):
model `claude-haiku-5-5`, reward **1.0**, 338s wall (spec
`01M4T2ZG0K8EST53BG4TVTG4PP`). Token usage (`result.json` `agent_result`):
65,398 input (42,747 cached reads + 22,645 cache creation + 6 fresh), 1,106
output, $0.0038 API list-price equivalent — not billed. The bridge ledger
agrees exactly (3 calls, all 200, served `anthropic/claude-haiku-5-5` every
time, no rate limits). Quota snapshot: none exists — `omp auth-gateway
check` needs a broker and there is none in stdio mode, and neither gateway
responses nor session files carry a weekly-window percentage, so Claude
headroom stays `[unavailable]` with its reason; per-trial tokens are
recorded instead. Credential scan: the trial's post-run scan passed, and a
follow-up audit found no `sk-ant-` strings, no token variable values, and no
token-shaped strings anywhere under the job dir (only variable *names* in
the captured source diff). Lane quota tally for the night: 5 tiny Haiku
probes plus this trial, all far below any observable limit.

Session-transcript accounting note, established against this trial:
`evallab.quota` fills a claude-code trial from `result.json`, falling back
to the CLI session transcript (`message.usage` scalars only). The fallback
counts each distinct usage tuple once: this trial's transcript replays one
response three times under fresh uuids, and naive summation would have
tripled it (verified: collapsed fallback reproduces 65,398 / 42,747 /
1,106 exactly).

Alternative (unproven): the Keychain lane — `scripts/with-claude-auth` reads
`harbor-practice-claude-oauth` into `CLAUDE_CODE_OAUTH_TOKEN` (plus
`CLAUDE_FORCE_OAUTH=1`); models `anthropic/claude-haiku-5-5` and
`anthropic/claude-fable-5`. Blocked until Peter mints the token:
`scripts/claude-token-setup.sh` (browser OAuth, hidden input, never
printed).

## Defaults by job

| Job | Default (subscription) | Fallback (open models only) | Evidence | $ per 1k |
|---|---|---|---|---|
| Bulk judge (first pass over every transcript) | Claude Haiku 5.5 via the OMP gateway | none for rate limits (back off); GLM-5.3-Flash only where Claude is unsuitable (e.g. repeated refusals), via the CheatBench judge's Z.ai route (balance empty since 2026-10-09) | Haiku 5.5 agrees with Sonnet on 82% of CheatBench trials, catches 37/40 labelled hacks; 8% unparseable replies need the adjudicator | $4.40 equivalent (CheatBench prompt), $1.21 (scorecard prompt); GLM $6.40 billed |
| Adjudication judge (Haiku-clean, unparseable, disputed) | Claude Sonnet 5.5 via the OMP gateway | same rule; GLM-5.3 where Claude is unsuitable | Sonnet 5.5: 48/49 on CheatBench, agrees with the detector and every stored judge | $88 equivalent per 1k adjudicated; GLM-5.3 $60 billed |
| Cheap cheater (prompted to find and use holes) | Codex `gpt-6-luna` via `codex exec` (proven 2026-10-13: reward 1.0 on `event-summary`, sandbox CLI 0.162.1; `gpt-5.6-terra` ran on the old pin); second choice Claude Code with Haiku 5.5 | GLM-5.3-Flash (`openrouter-metered/z-ai/glm-5.3-flash`), proven in cheater-recall wave 1 and every CheatBench family | subscription cheaters are unproven: run a 10-task pilot before a campaign | gpt-6-luna ≈ $11 equivalent (Codex trial shape); GLM fallback $31 billed |
| Agent under test, a frontier model | that model's own CLI on its subscription: Codex (`gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`; both CLI pins now 0.162.1) or Claude Code (Sonnet/Opus 5.5) | none (frontier models are not run on OpenRouter while a subscription offers them) | Codex canaries: 146K input, 91% cached, 4.5K output per trial | gpt-5.6-terra ≈ $108 equivalent |
| Agent under test, an open model | no subscription offers these | OpenRouter pinned routes: GLM-5.3-Flash, DeepSeek V4.1-Flash, MiMo-V2.6-Flash, gpt-oss-120b | see the route table below | $11–53 billed |
| Agent under test, the student | MiMo-V2.6-Distill-Qwen-9B (+ adapters) on our Modal server, `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` | none | only route serving those weights | $30–150 of GPU time (20 vs 4.5 trials at once) |

## OpenRouter routes for open models

Pinned first-party endpoints, list prices read from OpenRouter's endpoint API
on 2026-10-12. Per 1k trajectories for two recorded shapes. The proxy settles
each OpenRouter call at the cost OpenRouter reports (`usage.cost`), so cache
hits and DeepSeek's peak schedule are billed as charged, not estimated.
Status is from the 2026-10-12 smoke (PR #849; total spend $0.0013).

| Model | Selector | Cheater attempt | Long repo task | Status |
|---|---|---|---|---|
| GLM-5.3-Flash, Z.ai | `openrouter-metered/z-ai/glm-5.3-flash` | $31 | $53 | works: terminus-2 smoke trial on `event-summary` passed (reward 1.0, 4 calls, $0.0012); default open cheater, proven in earlier campaigns |
| MiMo-V2.6-Flash, Xiaomi | `openrouter-metered/xiaomi/mimo-v2.6-flash` | $11 if cache hits are billed, $89 if not | $22 / $158 | answers; cache hits not yet observed (single-call smoke); endpoint uptime ~90% on 2026-10-12 |
| gpt-oss-120b, DeepInfra | `openrouter-metered/openai/gpt-oss-120b` | $25 | $43 | upstream returned 429 (shared pool overloaded) on both smoke calls; no cache pricing; 131K context per call |
| DeepSeek V4.1-Flash, DeepSeek | `openrouter-metered/deepseek/deepseek-v4.1-flash` | $17 (peak $35) | $27 (peak $55) | blocked: OpenRouter refuses this endpoint under the account's privacy setting (DeepSeek's first-party endpoint may train on prompts). Enabling it is Peter's call in OpenRouter's privacy settings; DeepSeek peak hours (Mon–Fri 01–04 and 06–10 UTC) cost double |

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
