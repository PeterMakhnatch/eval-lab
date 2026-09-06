---
title: "Repo2RLEnv Emitter Upstream Trial & First-Use Triple Audit"
date: "2026-09-06"
author: "BuilderWave0 (eval-lab harbor-native buildout)"
lane: "C3 — Synthetic Task Supply Line"
repo_target: "pallets/click (Python)"
status: "completed — verdict: REJECTED (non-admittable without architectural hardening)"
---

# Repo2RLEnv Emitter Upstream Trial & First-Use Triple Audit

## 1. Upstream Emitter Location & Architecture

- **Upstream Repository:** `huggingface/Repo2RLEnv` (Apache-2.0, 510★)
- **PyPI Distribution:** `repo2rlenv==0.8.2.post3`
- **Core Harbor Touchpoint:**
  - `repo2rlenv.emitter.harbor:write_harbor_task`: Materializes Harbor task packages (`task.toml`, `instruction.md`, `solution/patch.diff`, `solution/solve.sh`, `environment/Dockerfile`, `tests/test.sh`).
  - `repo2rlenv.pipelines.pr_diff:PRDiffPipeline`: Mines merged pull requests from GitHub/GitLab without requiring an LLM synthesis budget (`--pipeline pr_diff`).
  - `repo2rlenv.pipelines.pr_diff:build_pr_diff_environment_dockerfile`: Constructs a minimal `python:3.12-slim` image checking out the base commit.
  - `repo2rlenv.pipelines.pr_diff:_pr_diff_verifier.py`: 6-component reward verifier (`format_valid`, `size_sanity`, `file_targeting`, `region_overlap`, `similarity`, `llm_judge`).

---

## 2. Emission Run on Known Repository (`pallets/click`)

Executed upstream emitter against `pallets/click`:
```bash
uvx repo2rlenv generate --repo pallets/click --pipeline pr_diff --pipeline-opt limit=15 --out /tmp/click-tasks
```

### Generation Yield
- **Candidate PRs Evaluated:** 15
- **Skipped by Quality Filters:** 10 (4 `too_many_files`, 5 `docs_only_diff`, 1 `instruction_too_thin`)
- **Emitted Harbor Tasks:** 5
  - `pallets__click-3821`: `Add Option.get_help_spec to extract the help's left part of an hidden option` (46 LOC, difficulty medium)
  - `pallets__click-3818`: `Fix formatting of default value when default is empty string` (18 LOC, difficulty easy)
  - `pallets__click-3817`: `Preserve original case for choice options in error messages` (32 LOC, difficulty medium)
  - `pallets__click-3805`: `Support custom param types in command callback annotations` (54 LOC, difficulty medium)
  - `pallets__click-3801`: `Fix shell completion for zsh with subcommands` (62 LOC, difficulty hard)

---

## 3. First-Use Triple: Execution & Verification Results

All runs executed under Harbor 0.21.0 on local Docker (Apple Silicon arm64, zero model spend, zero API cost).

### Emitted-Task Verdict Table

| Task ID | Agent | Run Type | Reward | Exit Code | Wall Time | Observed Behavior |
|---|---|---|---|---|---|---|
| `pallets__click-3821` | `oracle` | Reference | **1.000** | 0 | 17s | `solution/solve.sh` applies `patch.diff`; verifier scores 1.0 |
| `pallets__click-3821` | `nop` | Zero baseline | **0.000** | 0 | 4s | No edits made; `predicted.patch` empty; reward 0.0 |
| `pallets__click-3821` | `cheat-probe` | Leak exploit | **1.000** | 0 | 5s | Agent reads `/verifier/oracle.patch` inside container; applies verbatim |
| `pallets__click-3821` | `cheat-tamper`| Tamper exploit | **1.000** | 0 | 5s | Agent overwrites `/verifier/verifier.py` to write 1.0 to reward.txt |
| `pallets__click-3818` | `oracle` | Reference | **1.000** | 0 | 7s | `solution/solve.sh` applies `patch.diff`; verifier scores 1.0 |
| `pallets__click-3818` | `nop` | Zero baseline | **0.000** | 0 | 5s | No edits made; reward 0.0 |
| `pallets__click-3818` | `cheat-probe` | Leak exploit | **1.000** | 0 | 5s | Agent applies `/verifier/oracle.patch`; verifier scores 1.0 |

---

## 4. Security & Integrity Vulnerability Analysis

The first-use cheat probes uncovered two critical structural vulnerabilities in the upstream Repo2RLEnv emitter:

### Vulnerability 1: Direct Oracle Ground-Truth Leakage
In `repo2rlenv/pipelines/pr_diff.py`:
```dockerfile
# Baked directly into the agent-accessible container
RUN echo "${encoded_oracle}" | base64 -d > /verifier/oracle.patch
RUN echo "${encoded_instruction}" | base64 -d > /verifier/instruction.md
RUN echo "${encoded_verifier}" | base64 -d > /verifier/verifier.py
```
Because the agent runs inside this container, `/verifier/oracle.patch` is world-readable. Any agent or cheat probe can execute `git apply /verifier/oracle.patch` without inspecting the issue description, scoring a perfect 1.000.

### Vulnerability 2: Missing Verifier Sandboxing (`environment_mode != separate`)
The emitted `task.toml` lacks Harbor 0.21.0's `[verifier] environment_mode = "separate"`. Consequently:
1. The verifier runs in the exact same container where the agent ran.
2. The agent executes as root in `/workspace` with full write permissions to `/verifier/verifier.py`.
3. An adversarial agent can rewrite `/verifier/verifier.py` to unconditionally write `1.0` to `/logs/verifier/reward.txt`.

---

## 5. Admissibility Verdict

### **Verdict: REJECTED (Non-Admittable into Eval-Lab Production / Capability Roster)**

The `Repo2RLEnv` `pr_diff` task family is **unsuitable for capability benchmarking or competitive agent evaluation** in its current upstream form.

### Prerequisites for Re-Admission:
1. **Separate Verifier Sandbox**: Upstream emitter must declare `[verifier] environment_mode = "separate"` and provide `tests/Dockerfile`.
2. **Scrubbing Verifier Assets from Agent Image**: `oracle.patch` and `verifier.py` must only exist in the verifier container, never mounted or baked into the agent's execution environment.
3. **Task Namespace Compliance**: Upstream emitter currently emits `default/<task_name>` unless explicitly configured with `--org`. Harbor 0.21.0 requires explicit `<org>/<task_name>`.
