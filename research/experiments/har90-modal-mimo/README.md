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

## Confirmation pair on merged main (2026-09-29)

0036-f and 0758-d ran on a6a78026 (#515) in parallel, on Daytona with the HAR-81 harness tree. Ceilings were 2,000 requests, 64M input and 1M output, with a 900 s timeout. The server took 203 s to warm and the `/v1/models` auth check returned 200. The app stopped at 04:30:33Z.

| | 0036-f | 0758-d |
|---|---|---|
| Reward | 0.0 (4/5 tests) | 0.0 (4/5 tests) |
| Verifier ran | yes | yes |
| Stop | `AgentTimeoutError` at 900 s | `AgentTimeoutError` at 900 s |
| Agent turns | 172 | 233 (56 + 177 after one summarization) |
| Parsed or mapped, live | 164/172 (95.3%) | 56/233 (24.0%) |
| Prose completions flagged | 2 (steps 45 and 173) | 0 |
| Ledger | 173 requests; 172 reconciled, 1 reserved | 237 requests, all reconciled |
| Lab outcome | failed: `proxy_usage_unreconciled` | done |

The acceptance bar was not met, and each failure has a cause the replays had not covered:

- **0758-d's parse rate.** After its summarization, every one of 0758-d's 177 turns used Claude Code's Bash-tool shape: `<function=bash>` with `command` and `description` parameters, two calls per turn, 354 calls in all. The normalizer rejected the unknown `description` parameter, so each of those turns was a parse error. The model repeated the same shape until the timeout.
- **0036-f's unreconciled call.** The agent timed out while call 173 was in flight. The runner stops the proxy after Harbor exits. Stopping killed that call's handler before the upstream reply arrived, so the call stayed `reserved` and the trial failed although the verifier had scored it.
- **0036-f's 8 parse errors.** These are 6 `write(file_path, content)` calls and 2 calls to a function named `keystrokes`. Both shapes are still left to Terminus's parse-error feedback: translating a file write into keystrokes is a semantic choice that nobody has made.
- **The confirmation loop.** The prose rule worked, but it didn't end 0036-f. At step 45, 6 minutes in, the model's prose summary was mapped and Terminus asked "are you sure". The model answered with a command. It then ran `echo done` 118 times over the last 9 minutes, and its second mapped prose turn came 6 s before the timeout. Research-Harbor kept Terminus's double confirmation; this trial is evidence on that choice.
- **A duplicated trajectory.** 0036-f's `trajectory.cont-1.json` repeats `trajectory.json` step for step, with the same session. Consumers must count each step once; HAR-92 covers this.

### Confirmation pair spend

- **Modal:** $0.9046, the whole 04:00 UTC bucket of the billing report: one warm period and both trials.
- **Daytona, by the formula:** about $0.043, from 1,873 s of sandbox time.
- **HAR-90 total:** about $4.51 of $5 (Modal $4.3562, Daytona about $0.15). The remaining ~$0.49 does not cover another pair.

## Follow-up 3: in-flight calls at the timeout and described bash calls (2026-09-29)

- **Drained proxy stop.** On SIGTERM the proxy stops accepting and lets in-flight calls run on for up to 120 s (`SHUTDOWN_DRAIN_SECONDS`). Each handler reconciles its call before replying, so a call whose client has gone still records its real usage. A call still in flight after the deadline is marked unresolved with `reason: in_flight_at_shutdown` and fails the trial's accounting, as before. The runner now waits 130 s for the proxy to stop.
- **`description` parameter.** It only labels a call, so the normalizer drops it and runs the command. Any other unknown parameter still rejects the turn.

### $0 checks

- **Replay** through Harbor 0.21.0, all eight trajectories, counting each step once:
  - 0758-d goes from 56/233 to 233/233, with 414 commands (354 described calls plus 60 with `duration`).
  - 0036-f stays at 164/172.
  - Overall, 2,093/2,310 becomes 2,270/2,310 (98.3%). The same 52 turns are mapped to `task_complete`: 50 in 0036-e and 2 in 0036-f.
- **Real proxy subprocess, drained.** The upstream took 1 s and the client gave up after 0.2 s, leaving the call `reserved`. After the runner's `_stop_terminus_proxy`, the call was reconciled with its real usage and `_read_proxy_usage` accepted a ledger with 0 unresolved. The same test fails on the previous proxy with 1 unresolved.
- **Real proxy subprocess, deadline.** With the drain shortened to 1 s and an 8 s upstream, the stop took 1.4 s. The ledger stayed valid, with 1 unresolved call carrying `reason: in_flight_at_shutdown`.

## Second confirmation pair on merged main (2026-09-29)

Research-Harbor chose to run one more pair and raised the cap to $6.00. 0036-g and 0758-e ran on e404cb87 (#521) with the same tasks, harness tree, ceilings and 900 s timeout as the first pair. The server took 216 s to warm and the `/v1/models` auth check returned 200. The app stopped at 06:20:20Z.

| | 0036-g | 0758-e |
|---|---|---|
| Reward | 1.0 (5/5 tests) | 0.0 (4/5 tests) |
| Verifier ran | yes | yes |
| Stop | `AgentTimeoutError` at 900 s | `AgentTimeoutError` at 900 s |
| Agent turns | 325 | 83 (37 + 46 after one summarization) |
| Parsed or mapped, live | 18/325 (5.5%) | 83/83 (100%) |
| Prose completions flagged | 1 (step 17) | 0 |
| Ledger | 326 requests, all reconciled | 86 requests, all reconciled |
| Lab outcome | done | done |
| Summarization attempts / splits | 0 / 0 | 1 / 1 |

Neither `trial.log` contains "Context length exceeded" or "Even fallback chat failed". 0758-e's one split wrote a real continuation with a new session. It repeats no turns; its one copied handoff turn is marked `is_copied_context`. 0758-e failed `test_edge_cpp_contract_and_python_build`.

The drain and `description` fixes held: no call was left unreconciled, and 0758-e parsed every turn. The pair still missed the 95% bar, for a reason no earlier trial showed:

- **A `task_complete` tool call.** 0036-g had solved the task by step 17. Its prose summary was mapped, and Terminus asked "are you sure". The model confirmed with `<tool_call><function=task_complete><parameter=task_complete>true</parameter></function></tool_call>`. The normalizer maps no function named `task_complete`, so the turn was a parse error. The model sent that call 307 times until the timeout: twice bare, 304 times after a one-line prose summary, and all 307 of its parse errors were this shape. If that call were mapped to `task_complete`, every turn would parse, and the trial would likely have ended at the second confirmation, about 10 minutes before the timeout. No normalizer change was made; it would re-key HAR-81's pilot again.
- **Parameter names.** In both trials, `bash` calls used only `command` and `duration`. The model used no `timeout`, `run_in_background`, `description` or `write`. Across all ten trials, the only other native calls are `bash` with `description` (354, all in 0758-d), `write(file_path, content)` (6) and `keystrokes` (2), both in 0036-f, and this pair's `task_complete` (307).
- **Replay.** On e404cb87, counting each step once, the replay matches the live counts: 18/325 for 0036-g and 83/83 for 0758-e.

### Second pair spend

- **Modal:** $0.9047, the whole 06:00 UTC bucket of the billing report: one warm period and both trials.
- **Daytona, by the formula:** about $0.043, for two sandboxes of about 15.5 minutes each.
- **HAR-90 total:** about $5.45 of $6.00 (Modal $5.2609, Daytona about $0.19).

## Follow-up 4: the native completion call (2026-09-29)

Research-Harbor authorised this one-shape fix and a third and final pair, and raised the cap to $7.00.

- **`task_complete` call.** A turn whose only call is `task_complete`, with the argument `true` or no arguments, now maps to `task_complete: true` with no commands. Text before the call becomes the analysis. Terminus's double confirmation still applies. The turn is still rejected if the argument is `false`, there is any other argument, the completion call sits beside command calls, the call is cut off, or text follows it.
- **Re-key.** The fix changes the normalizer digest, so HAR-81's pilot has to re-key.

### $0 checks

- **Replay** through Harbor 0.21.0, all ten trajectories, counting each step once:
  - 0036-g goes from 18/325 to 325/325.
  - The other nine trials are unchanged.
  - Overall, 2,371/2,718 becomes 2,678/2,718 (98.5%).
  - 360 turns are mapped to `task_complete`: the earlier 52 prose turns, plus 0036-g's 1 prose turn and 307 calls.

## Third confirmation pair on merged main (2026-09-29)

0036-h and 0758-f ran on 6d9da960 (#526). The tasks, harness tree, ceilings and 900 s timeout were the same as in the earlier pairs. Main also carried HAR-92's recording (#524, #525), so these are the first trials with step layers recorded live. The server took 211 s to warm and the `/v1/models` auth check returned 200. The app stopped at 07:37:11Z.

| | 0036-h | 0758-f |
|---|---|---|
| Reward | 1.0 (5/5 tests) | 0.0 (4/5 tests) |
| Verifier ran | yes | yes |
| Stop | `AgentTimeoutError` at 900 s | `AgentTimeoutError` at 900 s |
| Agent turns | 191 | 157 (145 + 12 after one summarization) |
| Parsed or mapped, live | 190/191 (99.5%) | 152/157 (96.8%) |
| Prose completions flagged | 1 (step 46) | 1 (step 16) |
| Ledger | 192 requests, all reconciled | 161 requests, all reconciled |
| Lab outcome | done | done |
| Summarization attempts / splits | 0 / 0 | 1 / 1 |
| Step layers recorded live | 191/191 | 157/157 |

The pair met the acceptance bar: at least 95% of live turns parsed or mapped, both verifiers ran, and no call was left unreconciled. The replay through Harbor 0.21.0 matches the live counts exactly. Neither `trial.log` contains "Context length exceeded" or "Even fallback chat failed". 0758-f failed the same test as 0758-e, `test_edge_cpp_contract_and_python_build`.

- **Parse errors.** 0036-h's one parse error is a Terminus object without `commands` in its first turn. 0758-f's five are final prose summaries that quote the task's output inside a fenced `json` block. The prose rule rejects any reply containing a JSON object, so these reach Terminus's feedback as intended.
- **Parameter names.** Every `bash` call in this pair (195 and 155) carried `command` and `description`. That is the shape #521 made executable; without it, both trials would have parsed almost nothing. No `duration`, `timeout`, `run_in_background` or `write` appeared. No native `task_complete` call appeared either, so #526 is proven by replay alone.
- **The confirmation loop.** Both trials finished early and then ran into the timeout. 0036-h's prose summary was mapped 4.3 minutes in, and Terminus asked "are you sure". The model answered with `echo "Task complete"` on 144 of its remaining 146 turns and never repeated the completion. 0758-f did the same 1.8 minutes in, with 129 of its 142 later turns. 0036-f showed the pattern first. The verifier scores the final state, so rewards are unaffected, but each solved trial holds the GPU about 10 minutes longer and puts loop turns into any SFT export. Whether to end at the first mapped completion is Research-Harbor's decision, not made here.

### Third pair spend

- **Modal:** $0.8999 for this pair's app in the 07:00 UTC bucket: one warm period and both trials. That bucket also holds $0.2676 for an HAR-81 deploy of the same app name from its own worktree, 07:54 to 08:02Z; that cost belongs to HAR-81, not HAR-90.
- **Daytona, by the formula:** about $0.043, for two sandboxes of about 15.6 minutes each.
- **HAR-90 total:** about $6.40 of $7.00 (Modal $6.1608, Daytona about $0.23).

## Follow-up 5: route residuals on the HAR-81 pilot (HAR-100, 2026-09-29)

This replay cost $0 and used no new trials.

**Coverage.**
- All 44 HAR-81 pilot trials: 3,513 turns.
- The 11 HAR-90 trials that reached an agent turn: 2,305 turns. 0036-c and 0758-a failed authentication before their first turn, and 0758-b left no trajectory.

**Method.** Each turn is counted once across its trajectory parts. It is replayed through main's normalizer and Harbor 0.21.0's stock Terminus parser. The recorded `prose_completion` flag stands in for `finish_reason`.

**Check.** The pilot ran the same normalizer as main, and its replay matches the live parse decision on all 3,513 turns. Older HAR-90 trials ran earlier normalizers, so their rows below show what main would still reject.

| Shape | Pilot turns | HAR-90 turns | Prompt tokens | Where (trial: steps) |
|---|---|---|---|---|
| Unescaped `"` inside a string of a bare Terminus object | 69 | 0 | 2.17M | a2-arvo-42496599: head 11, 19, 27-91; a2-arvo-42485576: head 23, 32 |
| Unescaped `"` inside a Terminus object wrapped in a `command` call | 37 | 0 | 1.65M | candidate-2684: head 21, 24, 26, 35, 37-69 |
| `<function=command>` whose body is the raw command | 6 | 0 | 0.01M | candidate-2684: head 2-7 |
| Calls to non-shell tools (`read`, `write`, a `keystrokes` function) | 15 | 8 | 0.26M | a3/a4-arvo-18737, p-arvo-42528228, p-arvo-42496599, a3-candidate-1271; 0036-f |
| Prose summary quoting a JSON object | 1 | 5 | 0.09M | a2-arvo-42485576 head 34; 0758-f cont-1 10-16 |
| Rejected shell or completion call | 3 | 1 | 0.06M | p-arvo-18737 head 16-17 (`task_complete` call with `true</parameter>` and no opener); candidate-1048 head 39 (duration `0._host_only_networks\|0.1`); 0036-b head 6 |
| Terminus JSON missing fields | 3 | 1 | 0.06M | candidate-1559, a2/a3-candidate-1271, 0036-h |
| Invalid JSON, other | 1 | 0 | <0.01M | a2-arvo-42485576 head 2 |
| Prose reply, before the prose rule existed | 0 | 50 | 1.87M | 0036-e head 35-84 |
| Harbor's fallback reply, not model output | 0 | 30 | 0 | 0758-c cont-31 184-213 |

- **Raw newlines inside JSON strings.** The normalizer has decoded raw control characters since #512, and the replay finds no turn still rejected for that reason alone. 186 turns carry such characters: 185 are accepted today (7 of them in the pilot), and the remaining one is also broken by an unescaped quote. So no code change was needed. The 67 turns in a2-arvo-42496599 are unescaped quotes: 65 of them repeat one message byte for byte, which contains `echo 'no git'"; git log …`.
- **Raw `command` calls.** These are now mapped. A `command` call whose body is plain text runs that text as one command. A `command` call with a JSON body is left to the stock parser, which already accepts the 25 valid ones in candidate-2684. The replay before and after the change:
  - the 6 turns in candidate-2684 now parse;
  - no other decision changes across all 5,818 turns;
  - the pilot goes from 3,378/3,513 (96.16%) to 3,384/3,513 (96.33%).
- **Pins.** The change alters `parser_digest` and HAR-81's `normalizer_sha256`, so the next pilot key must be re-pinned. Completed pilot data is unaffected, because reconstruction reads each turn's recorded verdict.
- **Not changed.** The confirmation loop and the unescaped-quote shape are costed as proposals on HAR-100 and HAR-96. Both need a decision from Peter and Research-Harbor.

## Follow-up 6: unescaped inner quotes and copied continuation steps (HAR-100, 2026-09-29)

**Inner quotes are now read.** A Terminus object that fails to decode because a string holds unescaped `"` is read every way its quotes allow. The object may be bare or the JSON body of an `exec`/`exec_command`/`bash`/`command` call. Each unescaped quote may end its string or be part of it.
- Only readings with exactly Terminus's keys and field types count, because MiMo never writes other keys.
- The reading that treats the fewest quotes as text wins. Readings with more swallow real structure: in a2-arvo-42496599 head 27 the choice is 1 quote or 7, and with 7 the first command's keystrokes run on through the second command.
- The turn stays rejected if two readings tie, if no reading fits, or if the search passes its budget of 4,096 quotes. The largest search in this data used 345.
- The winning reading is re-serialized, so raw newlines and text after the object drop out, as they do for the other shapes.

**Replay before and after.** The method is Follow-up 5's: 5,818 turns, main (`13ceb070`) against this branch. These are counterfactual decisions, not reruns.
- 105 decisions change. Every one goes from parse error to commands, and every one is in the HAR-81 pilot:
  - a2-arvo-42496599: head 11, 19 and 27-91 (67 turns);
  - candidate-2684: head 21, 24, 26, 35 and 37-69 (37 turns);
  - a2-arvo-42485576: head 32 (1 turn).
- Turns now accepted in the pilot go from 3,384/3,513 (96.33%) to 3,489/3,513 (99.32%). HAR-90 stays at 2,210/2,305 (95.88%).
- No other decision changes, and no turn the stock parser already accepted changes its commands.
- The 105 turns carried 3.78M prompt tokens. They are 10 distinct messages: one was sent 65 times in a row and another 32 times.
- Still rejected:
  - a2-arvo-42485576 head 23: `</` follows the analysis and there is no `plan`.
  - a2-arvo-42485576 head 2: a stray `""]` leaves no reading.
- candidate-2684 head 35 was the one turn that also had raw newlines. It is now read, so raw newlines (b) remain a no-op.

**The keystrokes are the model's own.** The 65 repeats in a2-arvo-42496599 send `… || echo 'no git'"; git log --all …`, and its stray `"` leaves bash waiting at a continuation prompt. The live run returned a parse error 65 times instead. What the model would have done next is not recoverable from a replay. The other 40 recovered turns have balanced quoting (checked with `shlex`, not bash).

**Copied continuation steps never executed.** candidate-2684's `trajectory.cont-1.json` step 3 (message sha256 `33407024f282…`) is flagged `is_copied_context`, and its message is absent from the head. It is not a live turn: it is byte-identical to step 2 of `trajectory.summarization-1-questions.json`, the questions subagent's reply. Harbor puts that reply into the continuation's chat as text for the answers subagent and never parses or executes it.
- Its commands read `vendor/bandit/core/tester.py`. The keys `job.log` sends after the handoff (lines 248-249) are step 5's corrected `vendor/bandit/bandit/core/…` paths.
- The survey covered all 13 continuations with copied agent steps (10 pilot, 3 HAR-90). Each has exactly one such step, step 3; each is that trial's questions reply, and none is in its head.
- Four keystrokes from those steps appear verbatim in `job.log`, in arvo-41330, a2-candidate-1271 and a4-arvo-42485576. Each send is already counted in a live step's recorded `keystrokes_sent`.
- The `copied` label is therefore right, and the report already excludes these steps.
- What was wrong was the reason text, which said the evidence lives in the head segment. It now names the questions file instead. Layers stored in completed trials keep the old text.

**Pins.** `parser_digest` and HAR-81's `normalizer_sha256` change again, so the next pilot key must be re-pinned. Completed pilot data is unaffected.
