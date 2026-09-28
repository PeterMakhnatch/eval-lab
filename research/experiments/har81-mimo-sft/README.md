# HAR-81: MiMo-V2.6 Harbor environments → teacher rollouts → Tinker SFT → held-out eval

**Question:** does LoRA SFT on verifier-passing Terminus-2 teacher trajectories from the MiMo-V2.6 Harbor environments raise an open student's pass rate on a sealed held-out split of the same environments?

**Status (2026-09-28):**
- Infrastructure is merged: #501 read-only snapshot staging, #504 Tinker Terminus route, #503 split, export and SFT launcher.
- The split below is sealed. The pilot is prepared but **not submitted**. $0 spent.
- Peter chose "no spend yet" in the operator chat. docs/NOW.md stays at no paid calls and no training.
- Each paid phase below needs his explicit go-ahead, and each spec needs `approve --actor peter`.

## Fixed design (one variable per comparison)

| part | pin |
|---|---|
| tasks | HAR-82 pinned snapshots `derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-<domain>@<rev12>/tasks/` (catalog `derived/parquet/external/task_catalog/task_versions.parquet`) |
| split | `split.json` (below) |
| harness | Terminus-2 (`SecretSafeTerminus2`) with `harness-teacher/` (sha256:f6ad892d…) or `harness-student/` (sha256:cd36ef47…). Both set `trajectory_config {raw_content, linear_history}` for SFT export. The student also sends `reasoning_effort: "none"` (thinking off) in the request body |
| teacher | `zai/glm-5.3-flash`. Alternative: `zai/glm-5.3` at about 9× the token price |
| student | `tinker/Qwen/Qwen3.6-35B-A3B`. Alternatives: `Qwen/Qwen3.5-9B`, `Qwen/Qwen3.8-27B` |
| tuned student | the same base as `tinker/<base>@tinker://<run>:train:0/sampler_weights/<step>`, launched with `evallab tasks replay <base spec> --model …` so only the weights change |
| backend | Daytona for model runs, local Docker for nop controls only |
| SFT | `evallab.sft_terminus export` → `evallab.sft_tinker dry-run` → `train --confirm-spend`. Uses tinker-cookbook 0.5.7 `chat_sl`, LoRA rank 32, lr 1e-4, batch 256, all assistant messages, renderer `qwen3_5_disable_thinking` |

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

Held-out tasks never enter a teacher pool, SFT set or RL batch; `sft_terminus export` refuses them.

Terminal held-out includes `candidate-0260-security-appsec`, whose grader is broken (see below). Scoring drops it through `evallab tasks catalog export-broken`, which leaves 15 scorable terminal held-out tasks.

HAR-85 still reads its own `har85.provisional_split/v1`. Switching to this split is that lane's change.

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

What these controls do and do not show:
- Terminal graders partly pass on an untouched workspace but never fully pass, so reward stays 0.
- The cyber nop stops before any PoC binary runs, so it proves setup and grading plumbing only.
- Cloud nop qualification belongs to HAR-88 (`../mimo-daytona-nop/`).

## Paid phases (staged or specified; none submitted)

Price basis:
- Daytona list rates: $0.0504 per vCPU-hour, $0.0162 per GiB-hour of memory, with 10 GiB disk billed.
- Model list prices: glm-5.3-flash $0.15 / $0.50 per M tokens; Qwen3.6-35B-A3B on Tinker $0.54 / $1.335 per M tokens.
- Expected values assume 40 turns at a 12K mean prompt (0.48M input, 0.02M output per trial), priced uncached. The pilot exists to replace this assumption with measured usage.
- Ceilings are what the proxy and queue enforce.

| phase | trials | expected | ceiling | gate |
|---|---|---|---|---|
| 1. pilot: teacher + base student on 16 train tasks (6 terminal, 10 cyber), 1 attempt each | 32 | about $8 | $47.52 | HAR-88 Daytona nop pass for terminal and cyber; Peter's approval |
| 2. teacher collection on the train split (terminal 48, then a cyber subset) | about 100 to 400 | about $0.10 per terminal trial, $0.16 per cyber trial | $1.11 / $1.31 per trial | measured pilot pass rate; beyond 1 concurrent cyber sandbox this needs Daytona Tier 2 (a $25 top-up) |
| 3. Tinker SFT (Qwen3.6, $1.177 per M train tokens) | n/a | about $18 per epoch at 300 trajectories × 50K tokens | set by `--epochs` × dataset tokens | the `dry-run` token count and a non-trivial export |
| 4. held-out eval: base vs tuned, paired | terminal 15 × 2 arms × 3 attempts = 90 | about $28 | $1.61 per trial | a checkpoint exists |

The pilot (phase 1), prepared from merged main and **not submitted**:

```bash
research/experiments/har81-mimo-sft/stage.sh cohort    # ($0) pilot-cohort.json: train minus export-broken, one task per split_group
research/experiments/har81-mimo-sft/stage.sh prepare   # ($0) 32 specs -> derived/prepared/har81-p-{t,s}-*.json
research/experiments/har81-mimo-sft/stage.sh submit    # queue; each spec waits for approval
# after Peter's go-ahead:
for id in $(cat derived/har81/pilot.ids); do uv run evallab approve "$id" --actor peter; done
uv run evallab tick --parallel 1
```

Pilot cost ceilings, from `arms.json`:
- Teacher: `cost_limit_usd` 1.0 at 5M input tokens. At the default token caps a flash trial cannot exceed $0.82.
- Student: `cost_limit_usd` 1.5 at 2.5M input tokens.
- Cyber specs carry `override_storage_mb: 10240`, because cyber tasks declare no disk while terminal tasks declare 10 GiB.

Daytona Tier 1 quota is 10 vCPU / 10 GiB / 30 GiB. That fits 3 terminal sandboxes (1 vCPU / 2 GiB / 10 GiB each) or 1 cyber sandbox (2 vCPU / 8 GiB) at a time.

Phases 2 to 4 reuse the merged commands:

```bash
uv run python -m evallab.sft_terminus export --root teacher=runs/<job> ... \
  --split-manifest research/experiments/har81-mimo-sft/split.json --out derived/har81/sft-v1
uv run python -m evallab.sft_tinker dry-run --data derived/har81/sft-v1 --model Qwen/Qwen3.6-35B-A3B
uv run python -m evallab.sft_tinker train --data derived/har81/sft-v1 --model Qwen/Qwen3.6-35B-A3B \
  --log-dir derived/har81/tinker-v1 --manifest-out derived/har81/tinker-v1.manifest.json --confirm-spend
uv run evallab tasks replay <held-out base spec> --name <n> \
  --model 'tinker/Qwen/Qwen3.6-35B-A3B@tinker://<run>:train:0/sampler_weights/<step>'
```

## Limits

- **No model has run on these environments here.** Pass rates, token use and the SFT data yield are all unknown until the pilot runs. If the teacher passes too few train tasks, SFT has no data, and phase 2 should stop there.
- **Thinking-off on Tinker is unverified.** Tinker documents `reasoning_effort` "none" but also says unsupported models return HTTP 400; nothing has been sent to it. On a loopback stub, Harbor 0.21.0 / LiteLLM 1.96.0 dropped the top-level knob and forwarded the `extra_body` value, which is why the student harness uses `extra_body`. The first student pilot call is the check: it must succeed and return no `reasoning_content`. The SFT renderer `qwen3_5_disable_thinking` must also match what Tinker serves.
- **Terminal statistical power is low.** 15 scorable terminal held-out tasks can only show large effects. Cyber has 103 held-out tasks in 21 groups, so its outcomes cluster within groups; analyze paired, by group.
- **Excluded domains.** Code (large images), general and webdev (HF_TOKEN judges; none is configured) and music (fails setup locally) are split but not staged.
- Expected costs are list-price estimates, not invoices. The proxy prices input uncached.
