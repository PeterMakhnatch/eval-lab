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
| | Split / harness | HAR-81 train split, not held-out; HAR-81 `harness-student` tree as of e76b691b (`raw_content`, `linear_history`; removed when HAR-81 moved to the shared `harness/` tree) | same |
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

## Follow-up: executing MiMo's native tool calls (2026-09-29)

Research-Harbor reopened HAR-90 and asked for three things: a deterministic normalizer on the Terminus-2 side, two reruns, and at least 95% of turns parsing with the verifier running. Two changes landed:

- **`evallab.mimo_tool_calls.MimoToolCallParser`**, route-only. It turns native calls into Terminus commands, decodes JSON with raw control characters allowed, strips native closing markup and appends the implied Enter. The raw output stays in every trajectory. See `docs/execution-tiers.md`.
- **The per-trial watchdog** now allows the agent timeout plus 600 s for Harbor's other phases (`RunRequest.trial_watchdog_seconds`). Before, it killed Harbor at exactly the agent timeout, so a trial that ran to its timeout never reached the verifier.

### Trials

All ran on Daytona with the HAR-81 `harness/` tree and the 900 s task timeout. Each pair ran concurrently on one warm server.

| Pair | Trial (spec) | What ran | End | Reward | Queue state | Turns | Tokens in / out |
|---|---|---|---|---|---|---|---|
| A | 0036-c `01M3NEJ704HHCQZQHH8FWPXCT6`, 0758-a `01M3NEJ7GYHG6129ZYQQW6YZQQ` | JSON-call normalizer | 401 on the first call | None | `failed` | 0 | 0 / 0 |
| B | 0036-d `01M3NEXDKQ3RSJS0RG3E4ZSRMZ` | JSON-call normalizer | agent timeout | 0.0 | `done` | 529 | 11,106,053 / 16,626 |
| B | 0758-b `01M3NEXEE0BCWZ2C45RDCJMK88` | JSON-call normalizer | agent timeout | 0.0 | `failed`, `proxy_usage_unreconciled` | 761 | 22,355,608 / 24,314 |
| C | 0036-e `01M3NG7CMNRKG7MK9Q7XXAEAA4` | + XML bash calls | agent timeout | **1.0** | `done` | 83 | 2,350,970 / 30,929 |
| C | 0758-c `01M3NG7DGDGCWYENFRPHFPWSTT` | + XML bash calls | agent timeout | 0.0 | `failed`, `proxy_usage_unreconciled` | 216 | 5,517,856 / 36,375 |

What happened in each pair:

- **Pair A** is an infrastructure failure. The key rotation used `modal secret create --from-dotenv`, which needs `python-dotenv`, and the tool's environment lacks it. The error was hidden, so the server kept the old key. The secret was rewritten with `--from-json`, and later runs checked `/v1/models` with the key before dispatch.
- **Pair B**: the model switched to Qwen3-Coder XML (`<function=bash><parameter=command>…`), a shape the normalizer did not yet handle. No command ran in 1,290 turns. The verifier still ran and scored both trials, which confirms the watchdog fix.
- **Pair C**:
  - 0036-e solved the task: reward 1.0, and its final answer came at agent +468 s.
  - 0758-c introduced a fourth shape: a bare Terminus object with raw newlines inside strings, followed by `</parameter><parameter=duration>0.5</parameter></function></tool_call>`. Every one of those turns failed strict JSON, and the model repeated one of them 176 times. The normalizer has handled this shape since pair C.
- **In all three pairs:** the served-name identity matched, `<think>` never appeared in `content`, and `reasoning_content` was rare (1–5 turns per trial).

### Parse rate: stock parser vs the current normalizer (replayed through Harbor 0.21.0)

| Trajectory | Stock | Normalized | Still failing |
|---|---|---|---|
| 0036 (trial 1) | 119/120 | 120/120 | – |
| 0036-b (trial 2) | 15/200 | 199/200 | 1 dangling list fragment |
| 0036-d | 0/529 | 529/529 | – |
| 0758-b | 0/761 | 760/761 | 1 Harbor fallback text |
| 0036-e | 1/83 | 33/83 | 50 identical prose final answers |
| 0758-c | 8/216 | 186/216 | 30 Harbor fallback texts |
| **All** | **143/1,909 (7.5%)** | **1,827/1,909 (95.7%)** | |

Two groups remain:

- **Harbor's fallback turns (31):** "Technical difficulties. Please continue with the task." is Harbor's own text after a failed model call, not model output. Without those turns the rate is 1,827/1,878 (97.3%).
- **Prose final answers (50):** the model's remaining gap. After solving 0036-e it answered with a prose summary and no tool call (its native end of episode) 50 times, until the timeout. The normalizer leaves prose to Terminus, which asks for JSON again.

### Context overflow on 0758

Both 0758 trials filled the 64K window after their parse-error loops:

- Terminus unwound the history to an estimated 11.4K free tokens. SGLang then counted 57,382 input tokens plus the 8,192-token completion, 38 over 65,536, and answered 400.
- A 400 carries no usage, so the proxy left those calls unresolved (13 on 0758-b, 93 on 0758-c). The runner then failed both trials with `proxy_usage_unreconciled`, although Harbor finished and the verifier scored them.

Normalized turns should stop these loops, but a long healthy session can still come within about 3K tokens of Terminus's estimate.

### Ceilings

HAR-81's `stage.py` pins 200 requests and 2.5M input tokens. Those ceilings need raising for this route:

- The healthy solve (0036-e) used 84 requests and 2.40M ledger input tokens in 900 s, 96% of the input cap.
- Trial 1 hit the cap at agent +590 s.
- A ceiling trip raises `RateLimitError`, which Harbor does not treat as an agent timeout, so the verifier never runs and the reward is missing (trials 1 and 2).

Tokens cost $0 here and time is billed, so the 900 s agent timeout already bounds spend. The ceilings should sit above what 900 s can consume: loops reached 779 requests and 26.2M input tokens. Pair C ran with 2,000 requests, 64M input and 1M output, and nothing tripped.

### Follow-up spend

- **Modal:** $1.9752, the whole 02:00 UTC bucket of the billing report, covering three warm periods and pairs A–C.
- **Daytona, by the formula:** about $0.087 (3,702 s of sandbox time over four full trials plus about 50 s for pair A).
- **HAR-90 total so far:** about $3.56 of $5 (Modal $3.4516, Daytona about $0.11).

## Follow-up 2: prose completions, ceiling trips and usage-less 400s (2026-09-29)

Research-Harbor chose option A with two guards (only `finish_reason: stop`, and a per-step mapping flag). It also asked for two fixes before HAR-81's first wave: a ceiling trip must still run the verifier, and a 400 without usage must not fail a trial that was scored. A third fix, making the normalizer a harness setting on both arms, is deferred while the Qwen arm is parked.

### Changes

- **Prose completion** (`evallab.mimo_tool_calls.prose_completion`). On the MiMo route, a turn counts as `task_complete: true` with no commands when all of these hold:
  - the completion finished with `stop`;
  - the text after any `<think>` block is not blank;
  - it has no tool-call markup and no JSON object;
  - it is not Harbor's "Technical difficulties" fallback.

  Terminus's confirmation turn still applies. `SecretSafeTerminus2` records `finish_reason`, which upstream's `LLMResponse` drops. Each mapped step gets `extra.prose_completion: true`, and each trajectory file's `final_metrics.extra.prose_completions` and the agent metadata's `prose_completions` hold the count.
- **Ceiling trips.** The proxy's 429 "trial budget exhausted" now ends the agent phase with `TrialBudgetExhaustedError`, a subclass of Harbor's `NonZeroAgentExitCodeError`. Harbor's single-step trial records that class and still runs the verifier. The metadata records `stop_reason: trial_budget_exhausted`, and cohort comparisons count it as budget exhaustion.
- **Usage-less 400.** The proxy settles it as a reconciled zero-token call with `error: provider_http_400_no_usage`. Other usage-less errors stay unresolved.

### $0 checks

**Replay through Harbor 0.21.0** (`TerminusJSONPlainParser` behind the new parser, all six trajectories):

| Parser | Parsed or mapped | Turns mapped to `task_complete` |
|---|---|---|
| Stock | 143/1,909 (7.5%) | 0 |
| Normalizer, turns not stopped | 1,827/1,909 (95.7%) | 0 |
| Normalizer, turns stopped | 1,877/1,909 (98.3%) | 50, all of them 0036-e's prose final answer |

The 32 turns that still fail are 31 Harbor fallback texts and the dangling fragment from 0036-b. No other turn across the 1,909 was mapped. Live, 0036-e would have ended at its second prose turn instead of 49 turns later.

**Real Harbor smoke.** A throwaway script drove the real `Terminus2` loop, the real `SecretSafeTerminus2` and the real proxy subprocess. It used a scripted loopback upstream and a fake tmux session:

- **Prose.** Turns: native call, then prose ending in `length`, then native call, then prose `stop`, then prose `stop`.
  - The truncated prose went through Terminus's max-tokens path and ended nothing.
  - The first stopped prose got the confirmation prompt, and the second ended the episode.
  - Steps 4 and 5 carry the flag, `final_metrics.extra.prose_completions` is 2, and the metadata's `prose_completions` is 2.
  - The raw prose stays in each step's `message`.
- **Ceiling.** With a request ceiling of 2, the third call got the 429.
  - The agent raised `TrialBudgetExhaustedError`, which is an instance of `NonZeroAgentExitCodeError`.
  - The metadata records `stop_reason: trial_budget_exhausted`.
  - The ledger has 2 reconciled calls and 0 unresolved.
- **Context overflow.** SGLang's 400 body came first, with summarization on.
  - The ledger has the 400 as reconciled with 0 tokens, followed by 2 successful calls: 0 unresolved.
  - The runner's `_read_proxy_usage` accepted the ledger, and the episode finished through the prose rule.

In every scenario, neither the provider key nor the capability appeared in any JSON evidence file.
