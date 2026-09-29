# HAR-81: MiMo-V2.6 Harbor environments → distill baseline → SFT → held-out eval

**Question:** how well does the self-hosted student `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` solve MiMo-V2.6 Harbor tasks under Terminus-2? And does SFT on its own verifier-passing trajectories raise its pass rate on a sealed held-out split?

**Status (2026-09-29):**
- Everything here is staged at $0. Nothing is submitted or approved.
- **Distill only** (Peter, about 03:45Z, via Research-Harbor): the distill is the only student. The Qwen3.5-9B/Tinker arm is parked. Its specs stay prepared, and `submit pair` no longer queues them; `--with-base` does, if Peter revives the arm.
  - Xiaomi's report already answers what that comparison was for. In its Table 6, base Qwen3.5-9B scores 19.5 on MiMo Code (mini) against the distill's 51.6, and 5.7 on Cyber against 31.3.
- **Overnight plan** (Research-Harbor, about 04:45Z, on Peter's about 04:35Z "get some of those tasks from mimo running but not too many"): Infra runs [wave A and wave B](#overnight-waves) with a cap of **$12 actual spend**, starting when Research-Harbor reports HAR-90 accepted. Every trial runs under one [treatment key](#treatment-key).
  - In HAR-90 the distill wrapped its turns in `<tool_call>` tags, and only 15 of 200 turns parsed as Terminus. That is why the waves wait for HAR-90's normalizer and ceiling-trip fixes to be accepted.
- Research-Harbor's requirements (about 03:35Z; parser parity dropped with the parked arm):
  1. Re-prepare from the current `export-broken`. `cohort.json` already pins it (3 tasks out, held-out 386); `prepare` re-runs after HAR-90's changes merge.
  2. Pre-register a rule for unqualified tasks. Done: [grader-broken suspects](#pre-registered-grader-broken-suspects).
  3. A context-overflow mitigation that would keep the arms equal. Done: [context overflow](#context-overflow-shared-harness).
- Research-Harbor's outcome rule (about 04:20Z), before wave 1. Done: [scored outcomes](#pre-registered-scored-outcomes).
- The held-out baseline waits for Peter. Trace tagging for these trials is HAR-91; Data's HAR-93 recomputes the treatment key independently.

Decisions this design follows:
- **Student and teacher** (Peter, 2026-09-28 about 22:40Z): the student is the distill, and there is no teacher. This supersedes the earlier glm-5.3-flash teacher + Qwen3.6-35B-A3B pilot, which e76b691b staged and this change removes.
- **Server and route:** HAR-90 (#506) built the Modal SGLang server and the Terminus-2 route.
- **Staging brief:** Research-Harbor, 2026-09-29 01:30Z. Authority: Peter's "let agents do w/e work they need" for $0 prep.

## Fixed design

| part | pin |
|---|---|
| tasks | HAR-82 pinned snapshots `derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-<domain>@<rev12>/tasks/`: terminal, cyber and code only |
| split | `split.json` (sealed, below) |
| cohort | `cohort.json`, written by `stage.py cohort` |
| harness | Terminus-2 (`SecretSafeTerminus2`), `harness/` tree sha256:2a8fd70d…. Temperature 0.6, top_p 0.95, `max_tokens` 4096, `proactive_summarization_threshold` 16384, `trajectory_config {raw_content, linear_history}` for SFT export. Thinking stays on |
| distill | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`. Modal A100-80GB, SGLang v0.5.20, 64K context. The proxy forces `enable_thinking`, T 0.6, top_p 0.95, top_k 20 |
| base (parked arm) | `tinker/Qwen/Qwen3.5-9B`, the distill's own base model. Tinker's default reasoning effort (0.9) means thinking is on, with `reasoning_content` split out |
| ceilings (both arms) | 200 requests, 2.5M input tokens, 131,072 output tokens per trial. Tinker `cost_limit_usd` $1.92 covers them. The distill's $0.01 is nominal: its tokens are priced at $0 |
| backend | Daytona Tier 2 (100 vCPU / 200 GiB / 300 GiB). The train check dispatches 3, then 8 at a time (`tick --parallel`, printed by `submit`); the held-out batch 16. Cyber and code specs carry `override_storage_mb: 10240` |

What the parked pair would isolate, if revived:
- The two arms share the harness digest, the ceilings and the tasks. What differs is the model: the distill is Qwen3.5-9B plus Xiaomi's post-training.
- Two known residual differences:
  - top_k: 20 for the distill; Tinker's default for the base.
  - wall-clock speed: the agent timeout is wall-clock, so a slower provider gets fewer turns.
- The parser also differs until HAR-90 item 4 (deferred) makes the MiMo tool-call normalizer a harness setting. On 5a6f4be9 (#512) it wraps the parser on the self-hosted route only.

## Context overflow (shared harness)

Research-Harbor asked for a mitigation that keeps the arms equal. Both settings below live in the shared harness tree, so the distill and the parked base get them under one digest: `stage.py prepare pair` renders identical Terminus kwargs, ceilings and harness sha256 for all 40 specs.

**Why overflow happens:**
- Both routes declare 65,536 tokens for input plus output (`MIMO_SELFHOSTED_CONTEXT_TOKENS`, `TINKER_CONTEXT_TOKENS`).
- Terminus counts tokens with LiteLLM's fallback tokenizer, which undercounts this model family. On HAR-90's 0758 trial it estimated about 54.1K where SGLang counted 57,382, about 6% more.
- Terminus summarizes proactively once its estimate leaves fewer than 8,000 tokens free: about 61K real tokens.
- With the default `max_tokens` 8192, any call over 57,344 real input tokens overflows. So a long session overflows before it ever summarizes: 0758-b and 0758-c got 13 and 93 HTTP 400s.

**`max_tokens` 4096** (default 8192) halves the output reservation.
- HAR-90's 1,912 distill turns (agent and summarization calls, thinking included) had completions of p50 31, p99 469 and max 1,571 tokens. None exceeded 2,048.
- Alone it is not enough. It moves the overflow line to 61,440 input tokens, which is about where summarization triggers, and the check runs before the pending observation (up to 10 KB) is added.

**`proactive_summarization_threshold` 16384** (default 8000) is the "smaller declared context" option, done in the harness so the route constants stay as HAR-90 built them.
- Summarization starts at an estimate of 49,152 tokens, about 52K real.
- The next call then carries at most about 52K + one observation (about 3K tokens) + the 4,096 reservation: about 60K.
- The summarization subagents fit too: history + summary prompt + a summary of at most 4,096 + the 4,096 reservation.
- That tolerates an undercount of up to about 13%, against the measured 6%.

**What it costs:**
- Summaries come earlier, so more trials will have continuation segments (`trajectory.cont-N.json`). `sft_terminus export` already exports each continuation as its own conversation.
- The distill's longest completion in HAR-90 was 1,571 tokens, but harder tasks may think longer. A reply cut at 4,096 tokens performs none of its actions: Terminus answers "ERROR!! NONE of the actions you just requested were performed…" and asks again. The train check's wave 1 measures this (see the stop rule).
- Qwen3.5-9B's thinking length under Terminus is unmeasured, so the equal cap may not have an equal effect. If the base arm is revived, its wave 1 checks it the same way.
- Serving 128K on the distill alone is out: it would break parity (the Tinker route is 65,536) and raise Tinker's token cost.

## Sealed split (`split.json`)

- `manifest_digest` sha256:c3df70a5551c47d6c695abb3b8e51ca009b48d606289de4f637a7c1329e49f58
- salt `har81-sealed-20260928`
- catalog sha256:0bf5ba10…d050f0 (7,780 rows)
- Held-out membership is assigned by whole `split_group`, never by task_id.

Reproduce it (a re-freeze from merged main is byte-identical):

```bash
uv run python -m evallab.sft_split freeze --salt har81-sealed-20260928 \
  --heldout-count terminal=16 --heldout-count cyber=100 --heldout-count code=270 \
  --heldout-count general=92 --heldout-count webdev=209 --heldout-count music=100 \
  --out research/experiments/har81-mimo-sft/split.json
```

| domain | tasks | groups | held-out tasks (groups) |
|---|---|---|---|
| terminal | 64 | 64 | 16 (16) |
| cyber | 1000 | 176 | 103 (21), because groups stay whole |
| code | 2698 | 2563 | 270 (256) |
| general | 925 | 693 | 92 (66) |
| webdev | 2093 | 2093 | 209 (209) |
| music | 1000 | 1000 | 100 (100) |

Held-out tasks never enter an SFT set, optimizer or RL batch; `sft_terminus export` refuses them.

HAR-85 still reads its own `har85.provisional_split/v1`. Switching to this split is that lane's change.

## Cohorts (`cohort.json`)

The pool is the sealed split minus `tasks catalog export-broken --backend daytona`. `cohort.json` pins the export (sha256:35f66da1…). That export removes three grader-broken terminal tasks from HAR-88: `candidate-0260-security-appsec`, `candidate-0674-ml-evaluation` (torch) and `candidate-2376-security-cryptography` (cryptography).

- **pair (20 train tasks):** 7 terminal, 7 cyber, 6 code. Tasks are ranked per domain by sha256("har81-distill\0" + task_id), one task per `split_group`. The distill's 20 runs are the train check. The parked base has a prepared spec for every task.
- **heldout (386 tasks):** every remaining held-out terminal (13), cyber (103) and code (270) task. The distill runs alone.

Both lists alternate domains (terminal, cyber, code, …), so the first lines of each ids file cover every domain. With `--with-base`, `submit` queues each task's two arms together.

## Pre-registered: grader-broken suspects

Research-Harbor set this rule on 2026-09-29, about 03:35Z. It is written here before any HAR-81 trial runs, and it applies to every HAR-81 trial: the train check, the held-out baseline, the post-SFT held-out run, and the base arm if revived.

**Coverage.** HAR-88's Daytona nop run qualified 22 of the 406 cohort tasks: every terminal task (20), plus 1 cyber and 1 code task, all held-out. The other 384 have never been graded on Daytona: 109 cyber and 275 code. Thirteen of them are in the train check (7 cyber, 6 code), including wave 1's cyber and code tasks.

1. **Suspect.** An unqualified task becomes suspect when any of its trials, in any run, shows either signal:
   - **(a) HAR-88's grader_broken rule.** In `evallab.task_qualification`, `detect_grader_collection_failure(grader_stdout_texts(trial_dir), instruction_text=read_task_instruction(trial_dir))` returns an error line. That means pytest could not collect a test module because of a `ModuleNotFoundError`, `ImportError` or `SyntaxError`. Its two guards still apply: a missing module that the instruction names, or an import that fails inside agent-editable workspace code, is not a grader defect.
   - **(b) The verifier failed without scoring.** HAR-88's `classify_trial` returns `verifier_error`: a verifier-phase exception other than a timeout, such as Harbor finding no reward file.
2. **Not suspect:** a scored reward of 0; a verifier timeout; a trial whose verifier never ran (a ceiling or agent failure; unscored, see [scored outcomes](#pre-registered-scored-outcomes)).
3. **Re-check.** Suspects are collected into one nop re-check on Daytona using HAR-88's recipe (`../mimo-daytona-nop/`), bundled into a later Peter approval. At HAR-88's measured sandbox cost, that is about $0.01 per code task and $0.003 per cyber task.
4. **Confirmed** means the nop trial comes out `broken` and `tasks catalog export-broken` lists the task.
   - A confirmed task is excluded from every before/after comparison: the baseline and post-SFT runs, and both arms if the base is revived.
   - The exclusion is per task. It never depends on which run raised it.
   - Re-run `stage.py cohort` afterwards so the export drops the task from later pools.
5. **Cleared** means the nop trial is `ok`. The task stays in, and the flagged trial counts as the agent's failure.
6. **Unresolved.** No before/after number is final while a suspect is unresolved. Interim reports list suspects separately, with the signal and the trial. If the re-check is not approved, suspects stay in, flagged, and the comparison is also shown without them.

Signal (b) is Infra's addition to the brief; Research-Harbor can strike it on review. Signal (a) only reads pytest output:
- Among the 384 unqualified tasks, 81 code tasks run pytest (the `mimo_test_command.sh` in `tests/test.patch`).
- The other 194 code tasks use Go, JS and other runners. All 109 cyber tasks grade with `verify.py`.
- These fail before scoring in other ways. The code `test.sh` exits without a reward when it can't reset the test files or apply the hidden tests ("testbed problem, not scored"). A crash in `verify.py` also leaves no reward.
- Without (b), 303 of the 384 tasks would have no grader signal. An agent can also cause (b), for example by breaking the repository so the hidden tests can't apply. The nop re-check separates the two cases.

## Pre-registered: scored outcomes

Research-Harbor set this rule on 2026-09-29, about 04:20Z. It is written here before any HAR-81 trial runs, and it applies to every HAR-81 trial.

1. **Scored** means the verifier wrote a reward: `verifier_result.rewards.reward` in the trial's `result.json`, or its reward file. HAR-88's `collect_trial` reads both.
   - The stop reason doesn't matter: the agent finishing (Terminus `task_complete` or the normalizer's prose completion), an agent timeout (`AgentTimeoutError`) and a ceiling stop all count, with the reward the verifier gave.
2. **Unscored** means no verifier reward, and only that. Unscored trials stay out of the pass rate's denominator. Each is reported with its cause, from HAR-88's `classify_trial`: `setup_failed`, `backend_quota`, `verifier_error`, `verifier_timeout` or `reward_missing`.
   - One exception: a trial flagged by grader-broken signal (b) on a task the nop re-check clears counts as a failure (rule 5 above). The grader worked, so the agent broke it.
   - HAR-90 item 5 (#515, a6a78026) ends a ceiling stop as `TrialBudgetExhaustedError` and still runs the verifier, so a ceiling stop is scored. Before #515 it left no reward.
3. **Pass rates come from the reward, never from `trial_diagnosis`'s outcome label.**
   - That label marks any trial with `exception_info` as `infra_failed` (`trial_diagnosis.py:1001-1013`), graded agent timeouts included.
   - The diagnosis still carries the reward.
4. **Stop reasons are reported separately.** For each run and arm, report the scored trials and their pass rate by stop reason, and the unscored trials by cause.

Why: HAR-90's two timed-out 0036 trials were both graded. 0036-e passed (reward 1.0) and 0036-d failed (0.0), yet `diagnose_trial` labels both `infra_failed`. Research-Harbor notes that timeouts are mostly failures, so dropping them would inflate the baseline. The Traces tab (HAR-91) found this.

## Treatment key

Research-Harbor's overnight plan (about 04:45Z) pins one treatment for every overnight trial. If anything below has to change, stop and pin a new key; never mix keys in one comparison.

`stage.py key` computes the key from the dispatching checkout and writes `derived/har81/treatment-key.json`. It refuses to run on a dirty tree. The key is `sha256` of the compact, key-sorted JSON of these fields (`json.dumps(fields, sort_keys=True, separators=(",", ":"))`):

| field | value (origin/main eb549306 plus this change) | source |
|---|---|---|
| `eval_lab_commit` | the dispatching checkout's commit, pinned here before wave A | `git rev-parse HEAD`; must equal every trial's `lab-metadata.json` `repository.commit`, with `dirty: false` |
| `normalizer_sha256` | `sha256:990d3d0e4cf5b444a687e80b407ea8e20cdfac4a0ee1e673499345edba5d9e00` | `src/evallab/mimo_tool_calls.py` (#512, #515) |
| `model` | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` | `execution_contracts.py` |
| `hf_revision` | `2367e865d009c13ac81713a2878291d33ab28177` | `tools/modal-mimo-serve/serve.py` `MODEL_REVISION` |
| `server_image` | `lmsysorg/sglang@sha256:00b02004501e402332827ffd5343a225a8960d99adc990b37d6f31085b8f6800` (v0.5.20-runtime) | `serve.py` `SGLANG_IMAGE` |
| `context_tokens` | 65,536 | `serve.py` `CONTEXT_LENGTH` |
| `sampling` | temperature 0.6, top_p 0.95, top_k 20, `max_tokens` 4096, `enable_thinking` true | the proxy forces the first three and thinking; `max_tokens` is the harness's. `key` fails if the harness's temperature and top_p differ from the forced values |
| `harness_tree_sha256` | `sha256:2a8fd70dbc164a10f397b0fb59e4e4a871f0f2beb0ea3613d67a855e3b12eda4` | `harness/` |
| `harness` | Terminus-2, Harbor 0.21.0, `proactive_summarization_threshold` 16384, `trajectory_config {raw_content: true, linear_history: true}` | harness config; `harbor --version`, which `lab-metadata.json` records as `tools.harbor` |
| `limits` | 200 requests, 2,500,000 input tokens, 131,072 output tokens. Agent timeout from each task's `task.toml`: 900 s terminal, 3600 s cyber and code | `stage.py` constants; `task.toml` |
| `backend` | `daytona` | spec `environment` |

The agent timeout is not a flat 900 s. The overnight tasks' own `task.toml` gives 900 s for terminal and 3600 s for cyber and code, and the specs and cost estimates use those values.

Enforcement:
- `submit` recomputes the key and refuses to queue if it differs from the pinned file.
- `submit` also refuses any distill spec whose model, backend, harness digest or ceilings differ from the key.
- `receipt` checks each job's `lab-metadata.json` (commit, clean tree, Harbor version) and experiment spec against the key.
- Deploy the Modal server from the dispatching checkout, so `serve.py`'s revision and image are the ones the key names.

The commit and the key digest are recorded here and on HAR-81 before wave A. Trials dispatch from that commit; the README change that records it lands after, so the commit cannot contain its own hash.

## Overnight waves

Authority: Peter, 2026-09-29 about 04:35Z, in the Research-Harbor chat: "i want to get some of those tasks from mimo running but not too many" and "come up with the tasks and just assign them to whoever you think fit". Research-Harbor set Infra's share of the overnight cap at $12 of actual spend (Modal plus Daytona, by HAR-90's formula until the Modal bill settles). Approvals use `--actor peter`; `evallab approve` has no reason field, so this authority is recorded here and on HAR-81.

Start: when Research-Harbor reports HAR-90 accepted (#515 merged and its confirmation pair passed).

**Wave A: the train check.** The 20 staged pair tasks, one distill attempt each. Run 3 first (one per domain), then the other 17. Expected $2.95; spec-estimate sum $11.81.

**Wave B: learnability under the fixed key (pre-registered).**
- The first 8 of the 20 in cohort order, each given 3 more distill attempts (attempts 2–4). That is 24 trials, and 4 attempts per task counting wave A.
  1. terminal `candidate-1789-security-appsec`
  2. cyber `arvo_18737`
  3. code `format-code-task-001520`
  4. terminal `candidate-1634-software-databases`
  5. cyber `arvo_42485576`
  6. code `format-code-task-000240`
  7. terminal `candidate-1271-media-games`
  8. cyber `arvo_42496599`
- Specs: `har81-l-d-a<attempt>-<task>`, prepared with the same arguments as wave A's.
- Cost: $2.65 expected at `c = 8`; spec-estimate sum $12.12.
- Skip wave B if wave A's actual spend is above $6.
- Its spec-estimate sum alone exceeds the cap, so it runs in three rounds of 8, one per attempt. `submit learn` prints each round. Before each round, compare the cumulative actual spend plus that round's spec-estimate sum (about $4) with the $12 cap. If the sum passes $12, stop.
- What it measures: for each task, how many of 4 attempts pass under one key. That separates tasks the distill solves reliably, sometimes or never, and so which tasks can supply SFT passes.

**Stop and report** if any of these happens:
- cumulative actual spend reaches $12;
- a wave passes 2× its expected cost: $5.90 for A, $5.30 for B;
- more than 25% of a wave's trials end without a verifier result (`receipt` flags it);
- more than 5% of the distill's replies in wave A's first 3 were cut at `max_tokens`. Revisit the cap before the other 17.

Stop the Modal app after every wave (`modal app stop evallab-mimo-v26-9b`) and confirm it shows 0 containers.

**Receipt per wave, as a HAR-81 comment:**
- the spec ids and the treatment key;
- for each trial: reward, stop reason, and whether a verifier result exists;
- suspects under the grader-broken rule;
- actual spend and the Modal state.

`stage.py receipt pair` (add `--first 3` for A's first 3) and `stage.py receipt learn` print the per-trial table, the unscored share and the formula spend. The server spend settles with `modal billing report`.

## Staged runs (prepared at $0; nothing submitted)

```bash
uv run python research/experiments/har81-mimo-sft/stage.py cohort            # cohort.json
uv run python research/experiments/har81-mimo-sft/stage.py costs             # the table below
uv run python research/experiments/har81-mimo-sft/stage.py prepare pair      # 40 specs: 20 distill + 20 parked base
uv run python research/experiments/har81-mimo-sft/stage.py prepare learn     # wave B, 24 specs
uv run python research/experiments/har81-mimo-sft/stage.py prepare heldout   # 386 specs
# at dispatch, on a clean checkout:
uv run python research/experiments/har81-mimo-sft/stage.py key               # pin the treatment key
uv run python research/experiments/har81-mimo-sft/stage.py submit pair       # wave A; prints the approve + tick commands
uv run python research/experiments/har81-mimo-sft/stage.py submit learn      # wave B, in 3 rounds
uv run python research/experiments/har81-mimo-sft/stage.py receipt pair      # per-trial receipt
```

Before submitting anything that runs the distill:
- Redeploy the Modal server from the dispatching checkout; HAR-90 left the app stopped. Then export `EVALLAB_MIMO_SELFHOSTED_UPSTREAM` and `MIMO_SELFHOSTED_API_KEY` to the executor. See `tools/modal-mimo-serve/README.md`.
- Stop the app after every wave.

### Cost formulas

Per trial, for each arm:
- distill (HAR-90's formula, `mimo_selfhosted_trial_cost_usd`): `2.8149 × trial_h ÷ c + daytona(sandbox_h)`, plus $0.40 per warm period (a 208 s cold start plus the 300 s idle tail).
  - `c` is the number of distill trials sharing the one Modal container. `submit` prints a `tick --parallel` that holds it:
    - wave A's first 3 run `c = 3`;
    - wave A's other 17 and wave B run `c = 8` at `tick --parallel 8` (16 with the parked base interleaved);
    - the held-out batch runs `c = 16`.
  - Trials at the tail of a batch share with fewer trials and so cost more.
- Qwen3.5-9B (parked): `0.66 × input_M + 1.995 × output_M + daytona(sandbox_h)`.

Daytona sandbox rates:
- terminal (1 vCPU / 2 GiB): $0.0834/h
- cyber and code (2 vCPU / 8 GiB): $0.2309/h
- 10 GiB disk on both

The spec estimate each spec carries is its worst case. It assumes:
- the ceiling spend is reached;
- the sandbox lives for agent timeout + verifier timeout + 900 s;
- for the distill, the server share covers the full agent timeout at the `c` of the spec's wave.

The largest distill spec estimate is $1.38 (code); the parked base's is $2.35. Both are under the queue's $3 per-job cap.

| segment | arm | n | c | expected | spec-estimate sum |
|---|---|---|---|---|---|
| wave A, first 3 | distill | 3 | 3 | $0.95 | $2.94 |
| wave A, other 17 | distill | 17 | 8 | $2.00 | $8.87 |
| wave B | distill | 24 | 8 | $2.65 | $12.12 |
| heldout | distill | 386 | 16 | $30.90 | $217.63 |
| pair wave 1 (parked) | Qwen3.5-9B (Tinker) | 3 | – | $1.20 at 0.48M input tokens per trial; $4.96 at HAR-90's measured 2.4M; $5.86 at the ceiling | $6.55 |
| pair rest (parked) | Qwen3.5-9B (Tinker) | 17 | – | $6.77 / $28.08 / $33.20 | $36.95 |

Expected values:
- Trial time is the mean of HAR-90's two trials, 539 s (631 s and 447 s), plus 5 minutes of sandbox setup.
- Each distill segment includes one warm period.
- Both HAR-90 trials ended at a ceiling before the verifier ran. Once the normalizer lets turns parse, trials may run longer; the spec estimates cover that up to the agent timeout.

The Tinker token range is wide: Peter's Search chat estimated $0.08–0.24 per run. At HAR-90's measured distill usage (2.4M input tokens on one terminal task), the base would cost about $1.60 per trial.

The [overnight waves](#overnight-waves) section holds the stop rules. Before A's other 17, compare the measured cost per trial with this table. For sandbox time use `stage.py receipt` or `evallab tasks qualify-collect --backend-rate-card daytona`; for the server use `modal billing report`.

## SFT for the distill

See `tools/modal-mimo-sft/README.md`: TRL LoRA SFT on one A100-80GB, using the distill's own chat template and assistant-only loss. It has a $0 dry run. Training and merging are gated behind `--confirm-spend`. Its input is an `evallab.sft_terminus export` of the distill's own reward-1 train trajectories, taken with `--keep-reasoning` so the training text matches the thinking-on serving format.

## $0 controls (local Docker, Harbor 0.21.0)

| job (`runs/…`) | task | reward | verifier |
|---|---|---|---|
| har81-nop-terminal-0036 | candidate-0036-software-data-engineering | 0 | 5 tests failed |
| har81-nop-terminal-0109 | candidate-0109-science-robotics | 0 | 5 failed, 2 passed |
| har81-nop-terminal-0260 | candidate-0260-security-appsec | 0 | **collection error** (`No module named 'stevedore'`). The grader is broken, so this is not a nop result; the task is excluded from pools |
| har81-nop-terminal-0758 | candidate-0758-ml-inference | 0 | 3 failed, 2 passed |
| har81-nop-terminal-1990 | candidate-1990-security-cryptography | 0 | 4 failed, 1 passed |
| har81-nop-terminal-2836 | candidate-2836-ml-kernels | 0 | 3 failed, 4 passed |
| har81-nop-cyber-{10353,42537851,391975647,11359} | arvo (skia, haproxy, libconfig, radare2) | 0 | healthcheck passed; `No PoC was submitted with submit.sh.` |

Cloud nop qualification belongs to HAR-88 (`../mimo-daytona-nop/`).

## Limits

- **Without a normalizer, the distill mostly measures a protocol mismatch.** In HAR-90 it kept emitting its native `<tool_call><function=exec_command>` wrapper instead of Terminus JSON; in one trial only 15 of 200 turns parsed. That is why these runs wait for HAR-90's acceptance. After it, check the train check's parse-error rate before approving the 386-task held-out baseline.
- **In-sandbox harnesses can't reach Modal.** Daytona Tier 1 and 2 restrict sandbox egress, so agents running inside the sandbox cannot call the Modal server. Terminus-2 calls the model from the controller, which is why it is the harness here.
- **Missing rewards.** Before #515, a ceiling made LiteLLM raise `RateLimitError` and Harbor skipped the verifier, so the reward was missing. #515 routes a ceiling trip through Harbor's agent-exit path, which still runs the verifier. Under the [outcome rule](#pre-registered-scored-outcomes) a trial with no reward is unscored, which drops mostly-failing trials from the denominator; `receipt` counts them beside the pass rate, and more than 25% stops a wave.
- **The parked base arm is untested.** Nothing has been sent to Tinker. If it is revived, its wave 1 must show that calls succeed, `reasoning_content` comes back separate and `content` parses. Tinker's OpenAI-compatible endpoint is a low-traffic beta; concurrent trials may hit 429s, which end a trial the same way a ceiling does.
- **The held-out baseline exceeds one day's $20 smoke budget** at the expected $30.90. Split it across days, or approve a subset.
- **The queue can't see the server bill.** The queue's $20/day ceiling adds each spec's estimate to spend already measured in the catalog. Distill tokens are priced at $0 and Modal server time never enters the catalog, so the ceiling does not bound the server cost. The operator's controls for it are `modal billing report` and stopping the app.
- **Statistical power.** 13 scorable terminal held-out tasks only show large effects. Cyber's 103 tasks sit in 21 groups, so analyze paired and by group.
- Expected costs are list-price estimates, not invoices.
