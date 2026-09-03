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
epistemic: MemGym C0 source-only ingestion adapter and fixtures repaired with exact identity, source authority, and semantic honesty on canonical base 9768ad60
collection: trajectory-analysis
reviewed: 2026-08-31
requested_by: Agent Data brief research/inbox/agent-data-close-memgym-semantic-honesty.md
evidence_pin: 50b404e6ae4e1fcd453d3e07963eb3e6312cbded
---

# Agent Data Reply: MemGym Source-Only C0 Ingestion (Identity, Authority & Semantic Honesty Repaired)

## Lane location

- Worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Branch: `data/memgym-source-ingestion`
- Base: `9768ad60a6d5a0cb90e4ff5dd1fbe116b050cc63`
- Upstream: `https://github.com/WujiangXu/MemGym` @ `50b404e6ae4e1fcd453d3e07963eb3e6312cbded` (tree `68c081f0271cfd7951e490afd59457b029ba0535`)

## Summary of Closures

1. **M1 — Exact Native Identity Types & Collision-Free Structured Composites:**
   - Native `task_id` preserves exact JSON scalar type (`str | int`, excluding bool). Conflicting types (e.g. training `0` vs result `"0"`) refuse fail-closed with `ValueError`.
   - String `domain` and `task_id` are preserved verbatim without `.strip()` or case-normalization.
   - Structured domain-separated composite IDs:
     - `trial_id`: `f"memgym:trial:{sha256(canonical_json({'domain': domain, 'task_id': task_id}))}"`
     - `operation_id`: `f"memgym:op:{sha256(canonical_json({'msg_index': msg_index, 'side': side, 'trial_id': trial_id}))}"`
     - Completely prevents delimiter collisions (`(domain='a:b', task_id='c')` vs `(domain='a', task_id='b:c')` yield distinct IDs).
2. **M2 — Remove Caller Identity Substitution:**
   - Removed arbitrary caller `trial_id` override. Derived solely from native fields.
   - Added `expected_trial_id` assertion parameter; caller string `"path:list-position:7"` fails closed against derived native identity.
3. **M3 — Exact Side/Session Admission:**
   - `steps[].side` strictly validated as exact member of `{"agent", "user"}` without `.strip()`.
   - Leading/trailing whitespace (`" agent "`), case variants (`"Agent"`), non-strings, and empty values fail closed.
4. **M4 — Direct Prompt Token Semantics:**
   - Null/absent `summarizer_prompt_tokens` $\rightarrow$ unavailable (`None`).
   - Present strict non-negative integer $\ge 0$ $\rightarrow$ preserved exactly, including `0` (released fixture's direct zeros preserved).
   - Present bool, numeric string, float, negative $\rightarrow$ fail closed (`ValueError`).
   - Updated `library/benchmarks/memgym.md` to remove unsupported `> 0` rule.
5. **M5 — Exact-Byte Source & Provenance Authority:**
   - Public API accepts captured `training_bytes: bytes | str | Path` and optional `result_bytes: bytes | str | Path | None`. Parsed mapping dict input is refused (`TypeError`).
   - SHA-256 is computed directly over captured bytes prior to parsing.
   - When `result_bytes` is absent, outcome provenance source and digest bind explicitly to training artifact (`training_source_ref` / `t_digest`), never defaulting to `result.json`.
6. **S1 — Exact Outcome String or Null (No str Coercion):**
   - Removed `str(raw_outcome)` coercion. `episode_outcome` preserves exact byte-decoded string or `None` if null/absent.
   - Present bool, integer, float, list, dictionary fail closed (`ValueError`), never creating Python repr text.
7. **S2 — Exact Compaction Marker (Refuse Malformed Values Before Mapping):**
   - When `steps[].memory.new_compaction` is present and non-null, exact boolean (`type(v) is bool`) is required.
   - `True` maps to compaction (digestless/typed-HOLD); `False` and absent/null map to session boundary.
   - Present integer `0/1`, string (`"true"`/`"false"`), float, list, dict fail closed (`ValueError`) before operation mapping.
   - `was_compacted` type is also strictly validated as boolean or null.

## Verification evidence

```text
uv run pytest -q -o addopts='' tests/test_producer_memgym.py tests/test_memory_continuity_producer.py tests/test_semantic_facts.py tests/test_feature_governance_control.py::test_feature_registry_zero_contract_errors
94 passed, 2 warnings in 0.32s

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
