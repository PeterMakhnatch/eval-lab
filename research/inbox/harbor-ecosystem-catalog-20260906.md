# Harbor ecosystem catalog — everything classified

Author: wK:p7 (Analyst). 2026-09-06. Built from: Peter's Exa runs aed186e6 + b763ef6e
(36 resources), my Exa agent run 4444cdcf (schema-constrained, official-docs-grounded),
my 8 targeted Exa searches, and the research-context library audit. Legend:
✅ verified against official repo/docs | 🔍 found via search, not yet opened | ⚠️ hold (license/audit).

## A. The core (everything hangs off this)

| Thing | What it is | Entry point | Status |
|---|---|---|---|
| harbor | The framework: run agents on containerized tasks, collect trials | `harbor run` | ✅ |
| harbor docs | Tasks, agents, run-jobs, training-workflows, hub, skills | harborframework.com/docs | ✅ |
| harbor-cookbook | Official recipes: multi-step tasks, GEPA, RL examples | github.com/harbor-framework/harbor-cookbook | ✅ |
| Terminus-2 | Reference terminal agent; emits ATIF; TrajectoryConfig for SFT | `--agent terminus-2` | ✅ |
| ATIF | Trajectory format (RFC 0001); validator included | `python -m harbor.utils.trajectory_validator traj.json` | ✅ |
| Agents roster | terminus-2, claude-code, codex, gemini-cli, openhands, pi, custom | `harbor run --agent <name> --model <litellm-model>` | ✅ |
| Sandbox backends | Docker (local), Modal, Daytona, E2B, Runloop, GKE | `--env` / config | ✅ |

## B. Benchmarks / task packs you can run (Docker + API model)

| Pack | What it measures | Where | Status |
|---|---|---|---|
| terminal-bench@2.0/2.1 | Hard real terminal tasks | github.com/harbor-framework/terminal-bench-2 | ✅ |
| Terminal-Bench 3.0 | Next-gen; task review automation, open contributions | github.com/harbor-framework/terminal-bench | ✅ |
| terminal-bench-science | Research workflows across sciences | github.com/harbor-framework/terminal-bench-science | ✅ |
| frontier-bench | Frontier agent work (harbor-framework org) | github.com/harbor-framework/frontier-bench | 🔍 |
| Harbor Index | Curated 80 tasks from 6000+, 5 trials/task, trajectory review | harbor index (registry) | 🔍 |
| TerminalWorld | 1,530 tasks from real terminal recordings (+200 human-reviewed, 20 sample) | HF EuniAI/TerminalWorld | 🔍 |
| FACET 6k | 6,020 Harbor task packages from skill pairs | HF FACET-Terminal/FACET-Terminal-Tasks-6k | 🔍 |
| MLS-Bench | ML/software tasks | harbor dataset | 🔍 |
| cline-bench | Coding-agent benchmark, Harbor spec | github | 🔍 |
| CVE-Bench | Security exploitation | github | 🔍 |
| PTXBench | Kernel agents | github | 🔍 |
| ScienceAgentBench adapter | Scientific computing/data analysis | adapter | 🔍 |
| data_agent_harbor_eval | Deterministic data-analysis tasks | github | 🔍 |
| Vendor packs | e.g. spacetimedb-terminal-bench | github | 🔍 |
| tau3-bench | Tool use / dual control (we already use) | harbor adapters | ✅ |

## C. Task generators (papers WITH released code/data — the "synthetic" cluster)

| Generator | Input → output | Code/data | Status |
|---|---|---|---|
| FACET (2608.18580) | skill pairs → env+instruction+solution+verifier, with repair loop | repo + 6k tasks | 🔍 |
| TerminalWorld (2605.22535) | terminal recordings → env, tests, instruction | repo + dataset | 🔍 |
| Repo2RLEnv | real repos → verifiable Harbor tasks | repo | 🔍 |
| SETA (2607.x) | skills → verifiable terminal RL envs (GRPO training) | repo | 🔍 |
| LiteCoder-Terminal | synthesizes envs → SFT trajectories + RL envs (602 envs/11k traces) | repo | 🔍 |
| Recursive Synthesis (2608.05466) | self-evolving verified long-horizon tasks | repo | 🔍 |
| Endless Terminals | procedural verifiable terminal envs | repo | 🔍 |
| Reading only (no runnable release for us): SPADE (2608.19197, RL self-play), TRACE (2604.05336, traces→deficits→envs), TASTE, Anchor (drift), FuncBenchGen | | | ⚠️ library has full cards |

## D. Writing tasks yourself (your "start writing tasks" lane)

| Tool | What it does | Entry |
|---|---|---|
| create-task skill | Agent walks you through authoring a Harbor task end-to-end | `npx skills add harbor-framework/harbor --skill create-task` |
| rewardkit skill | Write task verifiers with Reward Kit | `npx skills add harbor-framework/harbor --skill rewardkit` |
| publish skill | Publish to the Harbor registry | `npx skills add harbor-framework/harbor --skill publish` |
| Task tutorial + task structure docs | task.toml, instruction.md, environment/, solution/, tests/ | harborframework.com/docs/tasks |
| Multi-step tasks | Sequential steps, per-step tests, setup hooks | docs/tasks/multi-step |
| benchmark-template | 22 ci_checks/*.sh for task-set honesty | github.com/harbor-framework/benchmark-template |
| TB3 REVIEWING.md + TASK_REVIEW_AUTOMATION.md | The maintainer quality bar (oracle pass, NOP fail, mutants) | terminal-bench repo docs |
| Our equivalent | `synthetic_cert.py` 8-point certificate; library/tasks/ | eval-lab |

## E. Running experiments + interventions (no weights)

| Lever | How | Produces |
|---|---|---|
| Run config ablation | `--config`, `--n-concurrent`, `--max-episodes`, `--no-delete` | comparable jobs |
| Skills injection | `harbor run --skill ./skills/x` or `--skill org/repo@main` | measured skill effect on success/cost/behavior |
| Model swap | `--model <litellm>` | model contrast on same tasks |
| Agent swap | `--agent terminus-2|claude-code|codex|...` | harness contrast (same model) |
| GEPA prompt optimization | bespokelabsai/gepa-terminus2 `train.py --max-metric-calls 50`; cookbook gepa recipe | optimized prompts + logs |
| Optimizers-compound paper (2607.x) | read: do optimizers compound on TB2.0 | methodology |
| HarnessBridge (2606.x) | read: learnable controllers harness↔agent | methodology |

## F. Working with the data (analysis/observability)

| Tool | What | Entry |
|---|---|---|
| harbor view | local web UI: jobs, trials, trajectories, costs, comparisons | `harbor view jobs` |
| harbor hub job show | aggregate: cost, tokens, errors, retries | `harbor hub job show ID1 ID2` |
| harbor traces export | jobs → dataset rows (reward/success/metadata + ATIF + ShareGPT) | `harbor traces export --path <job> --filter success --sharegpt [--push]` |
| atif-lens | browser viewer for one trajectory | `npx atif-lens traj.json` |
| atif-view | python viewer | `pip atif-view` |
| vsc-atifviz | VS Code timeline | extension |
| Phoenix ATIF import | OTel span trees from ATIF | `upload_atif_trajectories_as_spans()` |
| deepagents analyze.py | reference job/trial analysis script | langchain-ai/deepagents |
| NeMo eval_harbor_atif_interop.ipynb | third-party agent eval via Harbor+ATIF | nvidia/nemo-agent-toolkit |
| Vestige | pass@k vs pass^k reliability stats | code d03e8a7 |
| Our stack | traj.py features, deficit miner, lessons views | eval-lab (already wired to this exact data) |

## G. Training / data recipes (export now, train later — external per charter)

| Thing | What | Status |
|---|---|---|
| Harbor SFT docs | trials → ShareGPT/HF; Terminus TrajectoryConfig(raw_content, linear_history) | ✅ |
| OpenThoughts-Agent (2606.24855) | full open data recipe: task mixing, teacher traces, filtering ablations; datagen notebook | ✅ repo+notebook |
| ADP (2510.24702) | interlingua for heterogeneous agent datasets → SFT formats | ✅ |
| TMax | terminal SFT/RL recipe | 🔍 |
| SkyRL / Polar / rLLM / NeMo Gym / Tinker / ApexAgents / LEGO-RL | RL stacks — READ ONLY for us (GPU) | 🔍 |
| RSIBench-Data | closed-loop SFT→Harbor eval→retain | 🔍 |

## H. Failure / recovery / reuse

| Thing | What | Status |
|---|---|---|
| Native failure export | `harbor traces export --filter failure` | ✅ |
| Trajectory seeding | `--load-trajectory traj.json` continuation (PR #2529) | 🔍 verify flag support |
| Recovery-Bench | failed-trace replay benchmark | ⚠️ STRICT HOLD (no root license; replay failed 11/20; library cards) |
| Papers: Recovery-Bench 2602.14922, AgentCheck 2607.11098 (fault injection, MIT engine), ToolMisuseBench | methodology | library has full cards |

## Where to start (3 lines, not advice-essays)

1. **Write tasks:** create-task + rewardkit skills; certify with our synthetic_cert; the quality bar is TB3 REVIEWING.md.
2. **Run experiments:** small TB2.1 slices with terminus-2 + cheap model, `--export-traces`; interventions = `--skill`, `--model`, GEPA prompts.
3. **Work with data:** `harbor traces export` + our traj.py/deficit miner on the same bytes; eyeball in harbor view / atif-lens.
