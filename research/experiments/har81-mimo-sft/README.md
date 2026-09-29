# HAR-81: MiMo-V2.6 Harbor environments → distill baseline → SFT → held-out eval

**Question:** how well does the self-hosted student `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` solve MiMo-V2.6 Harbor tasks under Terminus-2? And does SFT on its own verifier-passing trajectories raise its pass rate on a sealed held-out split?

**Status (2026-09-29):**
- Everything here is staged at $0. Nothing is submitted or approved.
- **On hold** (Research-Harbor, 2026-09-29): don't request approval for the pair or the held-out baseline until HAR-90's tool-call normalizer lands.
  - In HAR-90 the distill wrapped its turns in `<tool_call>` tags, and only 15 of 200 turns parsed as Terminus.
  - After the normalizer lands, re-run `prepare`, and re-check the costs against its first measured trials.
- Paid runs then go to Peter as costed approvals. Each spec also needs `approve --actor peter`.

Decisions this design follows:
- **Student and teacher** (Peter, 2026-09-28 about 22:40Z): the student is the distill, and there is no teacher. This supersedes the earlier glm-5.3-flash teacher + Qwen3.6-35B-A3B pilot, which e76b691b staged and this change removes.
- **Server and route:** HAR-90 (#506) built the Modal SGLang server and the Terminus-2 route.
- **Staging brief:** Research-Harbor, 2026-09-29 01:30Z. Authority: Peter's "let agents do w/e work they need" for $0 prep.

## Fixed design (one variable per comparison)

| part | pin |
|---|---|
| tasks | HAR-82 pinned snapshots `derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-<domain>@<rev12>/tasks/`: terminal, cyber and code only |
| split | `split.json` (sealed, below) |
| cohort | `cohort.json`, written by `stage.py cohort` |
| harness | Terminus-2 (`SecretSafeTerminus2`), `harness/` tree sha256:01daa201…. Temperature 0.6, top_p 0.95, `trajectory_config {raw_content, linear_history}` for SFT export. Thinking stays on |
| distill | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`. Modal A100-80GB, SGLang v0.5.20, 64K context. The proxy forces `enable_thinking`, T 0.6, top_p 0.95, top_k 20 |
| base (pair arm) | `tinker/Qwen/Qwen3.5-9B`, the distill's own base model. Tinker's default reasoning effort (0.9) means thinking is on, with `reasoning_content` split out |
| ceilings (both arms) | 200 requests, 2.5M input tokens, 131,072 output tokens per trial. Tinker `cost_limit_usd` $1.92 covers them. The distill's $0.01 is nominal: its tokens are priced at $0 |
| backend | Daytona Tier 2 (100 vCPU / 200 GiB / 300 GiB), dispatched with `tick --parallel 16`. Cyber and code specs carry `override_storage_mb: 10240` |

What the pair isolates:
- The two arms share the harness digest, the ceilings and the tasks. What differs is the model: the distill is Qwen3.5-9B plus Xiaomi's post-training.
- Two known residual differences:
  - top_k: 20 for the distill; Tinker's default for the base.
  - wall-clock speed: the agent timeout is wall-clock, so a slower provider gets fewer turns.

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

The pool is the sealed split minus `tasks catalog export-broken --backend daytona`. Today that removes `candidate-0260-security-appsec` (broken grader) only.

When HAR-88's Daytona qualification is ingested, re-run `stage.py cohort` and `prepare`. Its broken_on_daytona findings then drop out, and `cohort.json` records the export's sha256.

- **pair (20 train tasks):** 7 terminal, 7 cyber, 6 code. Tasks are ranked per domain by sha256("har81-distill\0" + task_id), one task per `split_group`. Both arms run every task.
- **heldout (388 tasks):** every held-out terminal (15), cyber (103) and code (270) task. The distill runs alone.

Both lists alternate domains (terminal, cyber, code, …), and `submit` queues each task's arms together. So the first lines of the ids file cover every domain.

## Staged runs (prepared at $0; nothing submitted)

```bash
uv run python research/experiments/har81-mimo-sft/stage.py cohort            # cohort.json
uv run python research/experiments/har81-mimo-sft/stage.py costs             # the table below
uv run python research/experiments/har81-mimo-sft/stage.py prepare pair      # 40 specs
uv run python research/experiments/har81-mimo-sft/stage.py prepare heldout   # 388 specs
# after Peter's go-ahead, per batch:
uv run python research/experiments/har81-mimo-sft/stage.py submit pair       # prints the approve + tick commands
```

Before submitting anything that runs the distill:
- Redeploy the Modal server; HAR-90 left the app stopped. Then export `EVALLAB_MIMO_SELFHOSTED_UPSTREAM` and `MIMO_SELFHOSTED_API_KEY` to the executor. See `tools/modal-mimo-serve/README.md`.
- Stop the app afterwards.

### Cost formulas

Per trial, for each arm:
- distill (HAR-90's formula, `mimo_selfhosted_trial_cost_usd`): `2.8149 × trial_h ÷ c + daytona(sandbox_h)`, plus $0.40 per warm period (a 208 s cold start plus the 300 s idle tail).
  - `c` is the number of distill trials sharing the one Modal container. `submit` interleaves the pair's arms, so:
    - wave 1 runs `c = 3`;
    - the rest of the pair at `tick --parallel 16` runs `c = 8`;
    - the held-out batch runs `c = 16`.
  - Trials at the tail of a batch share with fewer trials and so cost more.
- Qwen3.5-9B: `0.66 × input_M + 1.995 × output_M + daytona(sandbox_h)`.

Daytona sandbox rates:
- terminal (1 vCPU / 2 GiB): $0.0834/h
- cyber and code (2 vCPU / 8 GiB): $0.2309/h
- 10 GiB disk on both

The spec estimate each spec carries is its worst case. It assumes:
- the ceiling spend is reached;
- the sandbox lives for agent timeout + verifier timeout + 900 s;
- for the distill, the server share covers the full agent timeout at the `c` of the spec's wave.

The largest spec estimate is $2.35 (Tinker, code), under the queue's $3 per-job cap.

| segment | arm | n | c | expected | spec-estimate sum |
|---|---|---|---|---|---|
| pair wave 1 | distill | 3 | 3 | $0.95 | $2.94 |
| pair wave 1 | Qwen3.5-9B (Tinker) | 3 | – | $1.20 at 0.48M input tokens per trial; $4.96 at HAR-90's measured 2.4M; $5.86 at the ceiling | $6.55 |
| pair rest | distill | 17 | 8 | $2.00 | $8.87 |
| pair rest | Qwen3.5-9B (Tinker) | 17 | – | $6.77 / $28.08 / $33.20 | $36.95 |
| heldout | distill | 388 | 16 | $30.99 | $217.85 |

Expected values:
- Trial time is the mean of HAR-90's two trials, 539 s (631 s and 447 s), plus 5 minutes of sandbox setup.
- Each distill segment includes one warm period.
- Both HAR-90 trials ended at a ceiling before the verifier ran. Once the normalizer lets turns parse, trials may run longer; the spec estimates cover that up to the agent timeout.

The Tinker token range is wide: Peter's Search chat estimated $0.08–0.24 per run. At HAR-90's measured distill usage (2.4M input tokens on one terminal task), the base would cost about $1.60 per trial.

A stop rule, as in HAR-88: approve the pair in two waves.
- Wave 1 is one task per domain, both arms (6 trials). `submit pair` prints the approve loop for it (`head -n 6` of the ids file) and for the rest.
- Before wave 2, compare the measured cost per trial with this table. For sandbox time use `evallab tasks qualify-collect --backend-rate-card daytona`; for the server use `modal billing report`.

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

- **Without a normalizer, the distill mostly measures a protocol mismatch.** In HAR-90 it kept emitting its native `<tool_call><function=exec_command>` wrapper instead of Terminus JSON; in one trial only 15 of 200 turns parsed. That is why these runs wait for HAR-90's tool-call normalizer. After it lands, check the pair's parse-error rate before approving the 388-task held-out baseline.
- **In-sandbox harnesses can't reach Modal.** Daytona Tier 1 and 2 restrict sandbox egress, so agents running inside the sandbox cannot call the Modal server. Terminus-2 calls the model from the controller, which is why it is the harness here.
- **Missing rewards.** When a ceiling fires, LiteLLM raises `RateLimitError` and Harbor skips the verifier, so the reward is missing, not 0. The paired analysis counts it as a failure under budget and reports how many such trials there were.
- **Thinking on Tinker is untested.** Nothing has been sent to Tinker yet. Wave 1 is the check: calls must succeed, `reasoning_content` must come back separate, and `content` must parse.
- **Throttling.** Tinker's OpenAI-compatible endpoint is a low-traffic beta. Sixteen concurrent trials may hit 429s, which end a trial the same way a ceiling does.
- **The held-out baseline exceeds one day's $20 smoke budget** at the expected $30.99. Split it across days, or approve a subset.
- **The queue can't see the server bill.** The queue's $20/day ceiling adds each spec's estimate to spend already measured in the catalog. Distill tokens are priced at $0 and Modal server time never enters the catalog, so the ceiling does not bound the server cost. The operator's controls for it are `modal billing report` and stopping the app.
- **Statistical power.** 15 scorable terminal held-out tasks only show large effects. Cyber's 103 tasks sit in 21 groups, so analyze paired and by group.
- Expected costs are list-price estimates, not invoices.
