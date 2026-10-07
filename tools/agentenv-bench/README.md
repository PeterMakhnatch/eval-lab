# agentenv-bench

Model-free test bench for MiMo-V2.6 "general" (simulated workplace) tasks, with
an optional port of the task's world into Scale's AgentEnv framework
(`agentenv-framework==0.9.1275`). It answers one question per task: **is the
task solvable, and does its grader accept the right outcome and reject the
wrong ones?** It never calls a model, a judge, or a cloud sandbox.

Results and findings: `research/experiments/agentenv-mimo-bench/README.md`.

## What it does

- `world.py` reads a pinned Harbor task's setup bundle, downloads the world
  files it names from Hugging Face (`XiaomiMiMo/MiMo-V2.6-RL-oss` at the pinned
  revision), checks every size and hash, and gives each run a fresh copy.
- `s3k_1591/controls.py` holds scripted controls: `oracle` (the reference
  solution the dataset never shipped), `nop`, and mutants that each make one
  plausible mistake.
- `s3k_1591/grader.py` runs the task's own deterministic `check_code` from
  `verifier_meta.json` verbatim (hash-pinned), adds one labeled non-upstream
  world-isolation check, and a deterministic report proxy that stands in for
  the upstream LLM item. Verdicts are reported separately: `upstream` (task
  rules only), `bench` (rules + isolation), `with_report`.
- `generate.py` recomputes the task parameters from the pristine database
  (self-check: it must reproduce the upstream constants exactly) and emits
  data-level variants for every other case the same instruction applies to.
- `direct.py` runs every control on the upstream task and each variant in
  plain Python (stdlib only, seconds).
- `aenv/` runs the same controls inside AgentEnv on local Docker: the four
  MiMo systems become MCP environments behind AgentEnv's gateway, a scripted
  A2A agent (no model) plays the controls, `env_outcome_verifier` grades the
  real post-run SQLite files, and a `race` variant uses a gateway trigger to
  bump the case's row version mid-run.

## Commands

```bash
TASK=<derived root>/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-general@10b732c5079c/tasks/s3k_1591_hr_people_en_t1_rl_008

# Plain-Python controls (no Docker; needs Hugging Face access on first fetch)
uv run --project tools/agentenv-bench --locked python -m agentenv_bench.direct --task-dir "$TASK" --out runs/agentenv-bench/direct-<stamp>

# Variants only
uv run --project tools/agentenv-bench --locked python -m agentenv_bench.generate --task-dir "$TASK" --out runs/agentenv-bench/generated-<stamp>

# AgentEnv on local Docker (~7.5 min, builds images, tears down its containers)
uv run --project tools/agentenv-bench --locked python -m agentenv_bench.aenv.run --task-dir "$TASK" --out runs/agentenv-bench/aenv-<stamp> --variant all
```

Each command writes `receipt.json` (input hashes, per-control rows, verdicts,
UTC timestamps, tool versions) and exits nonzero if any control's verdict
differs from its expected one.

## Limits

- **Trusted scripted controls only.** Inside AgentEnv the agent container can
  reach every environment's data plane (`/agentenv data/get` returns the full
  databases) and the gateway's `/trajectory`; tool RBAC hides tools but does
  not isolate those endpoints. Do not use this port to evaluate an untrusted or
  model-backed agent without adding network isolation.
- The upstream task's fourth grading item is an LLM judgment of the final
  report. The bench does not call it; its report check is a lexical proxy.
- `s3k_1591` is the only task wired today; controls and graders are
  task-specific. It is in the HAR-81 train split.
- Version discovery: no BlueSky read tool exposes `row_version`, so the
  scripted solver probes it with non-mutating `dry_run` submissions (0..255).
