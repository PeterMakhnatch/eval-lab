# MemGym Benchmark Card & Source Provenance

## Primary Sources & Exact Identity

- **Paper:** [arXiv:2605.20833](https://arxiv.org/abs/2605.20833) (MemGym: Towards Interactive Context Optimization in Long-Horizon Agent Evaluation)
- **Repository:** [https://github.com/WujiangXu/MemGym](https://github.com/WujiangXu/MemGym) (author-owned, owner: `WujiangXu`)
- **Project Site:** [https://wujiangxu.github.io/memgym-site/](https://wujiangxu.github.io/memgym-site/)
- **Exact Upstream Commit:** `50b404e6ae4e1fcd453d3e07963eb3e6312cbded`
- **Exact Upstream Tree:** `68c081f0271cfd7951e490afd59457b029ba0535`
- **Commit Date:** 2026-06-02T04:31:47Z

## Licensing & Asset Discrepancy

- **Repository Code & Fixture License:** **Apache-2.0**
  - `LICENSE` digest: `sha256:04a6dfa6a8e2222a1dc9959758c94e29335eaa7bb782da9470788396aa5bf64f` (11,305 bytes, 201 lines, Copyright 2026 MemGym authors)
  - `NOTICE` digest: `sha256:67866b5f1f5c41843e68f5c435e529d5ec86af31c2fe5265de868fd7986fe989` (5,652 bytes)
  - Required attribution: Apache-2.0 with NOTICE-attribution obligations.
- **Paper vs. Repository License Discrepancy:**
  - Paper §C.3 (*Asset Licenses*) states: *"The MemGym wrappers, the paired-trajectory corpus, and the synthetic MemGym-CodeQA / MemGym-DR instances are released under MIT."*
  - The repository `LICENSE` file is strictly **Apache-2.0**. For code/fixture ingestion, the repository Apache-2.0 license governs.
- **Corpus License:** Missing / unverified. The paired-trajectory corpus is not hosted in the Git repository (`data/` contains only instance-ID lists). The Hugging Face organization (`huggingface.co/MemGym`) is uninspected and out of scope.

## Environment & Dependency Hermeticity

- **Lockfile:** None. No `uv.lock`, `poetry.lock`, or `requirements.lock` exists in the upstream repository.
- **Dependencies:** 8 unpinned requirement sets (`requirements.txt`, `requirements-amem.txt`, `requirements-hipporag.txt`, `requirements-mem0.txt`, `requirements-simplemem.txt`, `requirements-swe.txt`, `requirements-tau2.txt`, `requirements-webarena.txt`).
- **Installer:** `install.sh` (`sha256:01e04d5d0e10c8e8...`) clones OpenHands, tau2-bench, WebArena, and LLMLingua-2 from the network at install time.
- **Status:** **Non-hermetic**. Hermetic reproduction and benchmark registration are on HOLD.

## Evaluator Determinism per Track

- **SWE-bench Track:** **Deterministic** evaluation via upstream SWE-bench harness and grading (`gym/swe_bench/env.py:534-617`). Requires Docker.
- **WebArena Track:** **Deterministic** verifier with explicit no-LLM replay probe (`gym/webarena/replay_probe.py`). Requires browser and WebArena server.
- **tau2 Track:** **Mixed / Model-Judged**. `gym/tau2_bench/env.py:150-207` evaluates natural language assertions using an LLM judge (`nl_judge_model`). Judge parse failures result in assertion failures (`met=False`), making scoring non-deterministic.
- **Memory Strategies:** Summarizing strategies (`Tau2SummarizingMemory`, `Tau2StructuredMemory`) require LLM calls (`_call_llm_summarize`). `PassThroughMemory` (`memory/base.py:300`) is model-free.

## Local Vendored Fixtures

The exact released fixture from `tests/fixtures/trajectories/tau2_bench_run/memory/retail/0/` is vendored under `tests/fixtures/memgym/`:

| File | SHA256 Digest | Size (Bytes) | Role |
|---|---|---:|---|
| `LICENSE` | `04a6dfa6a8e2222a1dc9959758c94e29335eaa7bb782da9470788396aa5bf64f` | 11,305 | Upstream Apache-2.0 license |
| `NOTICE` | `67866b5f1f5c41843e68f5c435e529d5ec86af31c2fe5265de868fd7986fe989` | 5,652 | Upstream attribution notice |
| `0_training.json` | `85c55f353ec12712d0d208c401fa6dbedfdbabe4314d6f879656d4f49629680f` | 4,717 | Released tau2 retail training log |
| `0_replay.json` | `cce417c236c4cec0e6fdc3c134b24d32d571d904b7c5dcae2430bb5d9f0a1c7a` | 28,214 | Released tau2 retail replay log |
| `result.json` | `c74cd64ec2fdff2cfb107ddfc14f9b4b135f83038b971a893c1e47d20ac1d4c5` | 7,153 | Released tau2 retail evaluation result |
| `ATTRIBUTION.json` | metadata | 1,090 | Machine-readable provenance |

## Source Mapping & Ingestion Contract (C0)

- **`trial_id`:** Composed from `domain` + `task_id` (`memgym:{domain}:{task_id}`).
- **`session_id`:** `steps[].side` (`"agent"` or `"user"`).
- **`step_index`:** Strictly mapped from `steps[].msg_index`. Globally unique integer establishing total order across interleaved agent and user turns. `steps[].step` is rejected for total order as it restarts per side and collides.
- **`operation_id`:** Canonical composite `memgym:{domain}:{task_id}:{side}:{msg_index}`.
- **`operation`:** `session_boundary` for step/message boundary events.
- **`before_token_count` / `after_token_count`:** Direct mapping from `steps[].memory.original_tokens` and `filtered_tokens`.
- **`prompt_tokens`:** Direct mapping from `steps[].memory.summarizer_prompt_tokens` (when present, > 0, and exact integer).
- **Outcome:** `episode_reward`, `episode_outcome`, `result.reward`, and `result.success` extracted directly with cross-record task ID validation.

## Status & Scope Holds

- **C0 Step/Session/Token/Outcome Ingestion:** **GO** (implemented in `src/evallab/interpretation/producers/memgym.py`).
- **Compaction Payload Ingestion:** **HOLD**. MemGym outputs `forgotten_count`, not ordered `forgotten_message_indices`. `ContextOperationPayloadV1` cannot be constructed honestly without ordered indices. Compaction facts omit payload digests (`content_digest=None`).
- **Write/Read/Use Linkage:** **HOLD** (typed unavailable; MemGym emits no tool-call IDs or memory read/use operations).
- **Verifier Evidence Mapping:** **HOLD** (typed unavailable; evaluation fields in released fixture are null).
- **Benchmark Certification & Registration:** **HOLD** (non-hermetic dependencies, unverified corpus license, network installer).
- **Measurement & Campaign Activation:** **HOLD** (explicitly non-activated; no model/control runs).
