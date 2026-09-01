---
source_url: https://github.com/WujiangXu/MemGym
source_type: repo
retrieved: 2026-08-31
license_note: Internal research audit; MemGym upstream Apache-2.0 applies.
feeds:
  - parked
type: audit-reply
topic: memgym-source-ingestion
author: agent-data-engineer
date: 2026-08-31
status: distilled
epistemic: MemGym C0 source-only ingestion adapter and fixtures prepared on canonical base 9768ad60
collection: trajectory-analysis
reviewed: 2026-08-31
requested_by: Agent Data brief research/inbox/agent-data-prepare-memgym-source-ingestion.md
evidence_pin: 50b404e6ae4e1fcd453d3e07963eb3e6312cbded
---

# Agent Data Reply: MemGym Source-Only C0 Ingestion

## Lane location

- Worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Branch: `data/memgym-source-ingestion`
- Base: `9768ad60a6d5a0cb90e4ff5dd1fbe116b050cc63`
- Head: `d1e1b31dfad53ca9c7f09d11cfc82af6dfc259c7` (tracking `origin/data/memgym-source-ingestion`)
- Upstream: `https://github.com/WujiangXu/MemGym` @ `50b404e6ae4e1fcd453d3e07963eb3e6312cbded` (tree `68c081f0271cfd7951e490afd59457b029ba0535`)

## Summary of deliverables

1. **Adapter (`src/evallab/interpretation/producers/memgym.py`):**
   - Maps `steps[].msg_index` to `ContextOperationFact.step_index` as the globally unique total order coordinate across interleaved agent/user messages. Rejects `step` for total order.
   - Maps `steps[].side` to `session_id`.
   - Constructs composite `operation_id = f"memgym:{domain}:{task_id}:{side}:{msg_index}"`.
   - Maps token counts from `original_tokens` and `filtered_tokens`; prompt tokens from `summarizer_prompt_tokens` when exact integer > 0.
   - Compactions omit ordered forgotten indices (`ContextOperationPayloadV1` refused/unconstructed; `content_digest=None`).
   - Extracts task outcomes (`episode_reward`, `episode_outcome`, `result.reward`, `result.success`) with fail-closed task identity validation.
2. **Vendored Fixtures (`tests/fixtures/memgym/`):**
   - Exact upstream files from `tests/fixtures/trajectories/tau2_bench_run/memory/retail/0/`: `0_training.json`, `0_replay.json`, `result.json`, `LICENSE`, `NOTICE`, and machine-readable `ATTRIBUTION.json`.
3. **Benchmark Card (`library/benchmarks/memgym.md`):**
   - Documents arXiv `2605.20833`, repo pin/tree, Apache-2.0 license with NOTICE attribution, paper MIT vs repo Apache discrepancy, missing corpus license, non-hermetic installer, track determinism (SWE-bench deterministic, WebArena deterministic, tau2 model-judged), and C0 scope holds.
4. **Tests (`tests/test_producer_memgym.py`):**
   - 9 test cases covering fixture digest/byte verification, total ordering, token mapping, outcome extraction, continuity feature extraction (0 writes/reads/uses $\rightarrow$ observed with null link metrics), representation order invariance, fail-closed duplicate `msg_index`, bool-vs-int rejection, task ID mismatch, and compaction payload refusal.

## Verification evidence

```text
uv run pytest -q -o addopts='' tests/test_producer_memgym.py tests/test_memory_continuity_producer.py tests/test_semantic_facts.py tests/test_feature_governance_control.py::test_feature_registry_zero_contract_errors
77 passed, 2 warnings in 0.33s

uvx ty@0.0.71 check src/evallab/interpretation/producers/memgym.py src/evallab/interpretation/producers/__init__.py src/evallab/interpretation/producers/memory_continuity.py --output-format=concise
All checks passed!

uv run ruff check src/evallab/interpretation/producers/memgym.py src/evallab/interpretation/producers/__init__.py tests/test_producer_memgym.py
All checks passed!

uv run ruff format --check src/evallab/interpretation/producers/memgym.py src/evallab/interpretation/producers/__init__.py tests/test_producer_memgym.py
3 files already formatted

uv run python -m py_compile src/evallab/interpretation/producers/memgym.py src/evallab/interpretation/producers/__init__.py tests/test_producer_memgym.py
passed
```

**Guardrails:** Zero model/control calls, zero MemGym/benchmark execution, zero activation/registration, zero subagents spawned. Returned for System Architect review.
