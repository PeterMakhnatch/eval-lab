# HAR-81: MiMo-V2.6 Harbor environments → distill baseline → SFT → held-out eval

**Question:** how well does the self-hosted student `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` solve MiMo-V2.6 Harbor tasks under Terminus-2? And does SFT on its own verifier-passing trajectories raise its pass rate on a sealed held-out split?

**Status (2026-09-29):**
- Everything here is staged at $0. Nothing is submitted or approved.
- Paid runs go to Peter as costed approvals. Each spec also needs `approve --actor peter`.

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
- distill: `2.8149 × trial_h ÷ 16 + daytona(sandbox_h)`, plus $0.40 per warm period (HAR-90 measured the $2.8149/h container rate and the 208 s cold start).
- Qwen3.5-9B: `0.66 × input_M + 1.995 × output_M + daytona(sandbox_h)`.

Daytona sandbox rates:
- terminal (1 vCPU / 2 GiB): $0.0834/h
- cyber and code (2 vCPU / 8 GiB): $0.2309/h
- 10 GiB disk on both

The spec estimate each spec carries is its worst case. It assumes:
- the ceiling spend is reached;
- the sandbox lives for agent timeout + verifier timeout + 900 s;
- for the distill, the server is shared by 16 trials for the whole agent timeout.

| batch | arm | n | expected | spec-estimate sum |
|---|---|---|---|---|
| pair | distill | 20 | $1.88 | $7.93 |
| pair | Qwen3.5-9B (Tinker) | 20 | $8.03 at 0.48M input tokens per trial; $33.10 at HAR-90's measured 2.4M; $39.13 at the ceiling | $43.50 |
| heldout | distill | 388 | $33.62 | $217.85 |

Expected values assume 10-minute trials plus 5 minutes of sandbox setup, with the server shared by 16 trials. HAR-90's two trials ran 447 s and 631 s.

The Tinker token range is wide: Peter's Search chat estimated $0.08–0.24 per run. At HAR-90's measured distill usage (2.4M input tokens on one terminal task), the base would cost about $1.60 per trial.

A stop rule, as in HAR-88: approve the pair in two waves.
- Wave 1 is one task per domain, both arms (6 trials).
- Before wave 2, compare the measured cost per trial (`qualify-collect --backend-rate-card daytona` for sandbox time, `modal billing report` for the server) with this table.

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

- **The distill may mostly measure a protocol mismatch.** In HAR-90 it kept emitting its native `<tool_call><function=exec_command>` wrapper instead of Terminus JSON; in one trial only 15 of 200 turns parsed. A Terminus-2 baseline may therefore score format failure more than capability. Check the pair's parse-error rate before approving the 388-task held-out baseline.
- **In-sandbox harnesses can't reach Modal.** Daytona Tier 1 and 2 restrict sandbox egress, so agents running inside the sandbox cannot call the Modal server. Terminus-2 calls the model from the controller, which is why it is the harness here.
- **Missing rewards.** When a ceiling fires, LiteLLM raises `RateLimitError` and Harbor skips the verifier, so the reward is missing, not 0. The paired analysis counts it as a failure under budget and reports how many such trials there were.
- **Thinking on Tinker is untested.** Nothing has been sent to Tinker yet. Wave 1 is the check: calls must succeed, `reasoning_content` must come back separate, and `content` must parse.
- **Throttling.** Tinker's OpenAI-compatible endpoint is a low-traffic beta. Sixteen concurrent trials may hit 429s, which end a trial the same way a ceiling does.
- **The held-out baseline exceeds one day's $20 smoke budget** at the expected $33.62. Split it across days, or approve a subset.
- **The queue can't see the server bill.** The queue's $20/day ceiling adds each spec's estimate to spend already measured in the catalog. Distill tokens are priced at $0 and Modal server time never enters the catalog, so the ceiling does not bound the server cost. The operator's controls for it are `modal billing report` and stopping the app.
- **Statistical power.** 15 scorable terminal held-out tasks only show large effects. Cyber's 103 tasks sit in 21 groups, so analyze paired and by group.
- Expected costs are list-price estimates, not invoices.
