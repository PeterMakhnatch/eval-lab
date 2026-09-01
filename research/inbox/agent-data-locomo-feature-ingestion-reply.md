---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-31
license_note: Internal research audit; Eval Lab repository license applies.
feeds:
  - parked
type: audit-reply
topic: locomo-atif-memory-feature-ingestion
author: agent-data-engineer
date: 2026-08-31
status: distilled
epistemic: audited repository state rebased on canonical step_index spine 1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee with all final Architect blockers closed; no real LoCoMo ATIF observed
collection: trajectory-analysis
reviewed: 2026-08-31
requested_by: Agent Data repair brief research/inbox/agent-data-close-last-memory-continuity-blockers.md
evidence_pin: 1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee
---

# Agent Data Reply: Memory Continuity Ingestion Repair (Last Blockers Closed)

## Lane location

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/agent-data-locomo-ingestion`
- Branch: `data/locomo-atif-memory-ingest`
- Base: `1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee`
- Non-goals honored: no LoCoMo task packaging, registry, profile/proxy/campaign admission, or synthetic certificates; no model calls; no integration or merge.

## Summary of blocker closure

1. **A1 — Exact payload authority only:**
   - `_extract_payload_v1` admits only `arguments["payload"]`.
   - Deleted all fallback to `arguments["content"]` and flattened top-level field synthesis.
   - Declared digest paired with `content` or flattened fields refuses positive content binding (`missing_content_identity`).
2. **A2 — Universal use assignment:**
   - Every admitted `memory_use` is inspected in temporal `step_index` order.
   - Each use requires exactly one eligible unused preceding read with the same exact digest.
   - Zero eligible reads (including unmatched digests absent from linked reads), multiple eligible reads, or unmatched reuses return typed `unavailable` with all link/latency metrics null.
   - Preserves valid alternating one-to-one assignments.
3. **A4 — Exact operation names:**
   - Removed `.strip()` and whitespace normalization from operation-name admission.
   - Admits only exact canonical names, optionally after exactly one sanctioned exact prefix removal among `memory_mcp_`, `mcp_`, and `functions.`.
4. **A5 — Complete emitted fact identity & stable fact-set digest:**
   - Serialized complete canonical JSON representation of every emitted `ContextOperationFact` via `fact.model_dump(mode="json")` (covers all emitted fields including `configured_size`, `realized_size`, `prompt_tokens`, `before_token_count`, `after_token_count`, etc.).
   - Sorts complete canonical JSON strings for representation stability under container reversal even on tied invalid keys (`(step_index, operation_id)`).
   - Any change to any emitted fact field changes `fact_set_digest`.

## Focused evidence

```bash
uv run ruff format src/evallab/interpretation/producers/memory_continuity.py \
  src/evallab/interpretation/producers/__init__.py \
  src/evallab/interpretation/feature_registry.py \
  tests/test_memory_continuity_producer.py
uv run ruff check src/evallab/interpretation/producers/memory_continuity.py \
  src/evallab/interpretation/producers/__init__.py \
  src/evallab/interpretation/feature_registry.py \
  tests/test_memory_continuity_producer.py
uv run pytest tests/test_memory_continuity_producer.py tests/test_semantic_facts.py tests/test_feature_governance_control.py::test_feature_registry_zero_contract_errors -q
uvx ty@0.0.71 check src/evallab/interpretation/producers/memory_continuity.py \
  src/evallab/interpretation/producers/__init__.py --output-format=concise
uv run python -m py_compile src/evallab/interpretation/producers/memory_continuity.py \
  src/evallab/interpretation/producers/__init__.py \
  src/evallab/interpretation/feature_registry.py \
  tests/test_memory_continuity_producer.py
```

Result: **68 passed** (34 producer tests + 34 semantic-facts tests). Ruff format/check and `ty` typecheck clean.
