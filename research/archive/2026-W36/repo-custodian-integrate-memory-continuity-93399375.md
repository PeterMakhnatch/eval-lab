# Repo Custodian — integrate approved memory-continuity producer

## Exact target

- Canonical worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Canonical branch/head: `analyst/synth-data-promotion-hardening @ 8fa4d4998b298ee4475eb55e30729f3ed8ef60d7`
- Approved source worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/agent-data-locomo-ingestion`
- Source branch/head: `data/locomo-atif-memory-ingest @ 93399375729226638c6a61a54e294521115edbde`
- Source base: `1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee`
- Final approval: `/tmp/system-architect-memory-continuity-final-review-93399375.md`

The canonical delta `1ecfc4a5..8fa4d499` touches only CLI/registry tests and does not overlap the approved five memory paths. Do not edit dirty root, integrate anything else, activate datasets, or run a model/control.

## Exact approved scope

1. `src/evallab/interpretation/feature_registry.py`
2. `src/evallab/interpretation/producers/__init__.py`
3. `src/evallab/interpretation/producers/memory_continuity.py`
4. `tests/test_memory_continuity_producer.py`
5. `research/inbox/agent-data-locomo-feature-ingestion-reply.md`

Diff versus source base: +1494/-66. Replay the full exact approved delta. No partial selection, broad formatting, compatibility shim, or test replacement.

## Contract to preserve

- Exact typed `arguments["payload"]` plus shared domain digest only; no content/flattened/context/raw fallback.
- Complete unique non-synthetic operation IDs; no positive facts on missing/duplicate identity.
- `ContextOperationFact.step_index` is the sole temporal coordinate; missing/duplicate order is typed unavailable.
- Every use requires exactly one eligible unused preceding read of the same digest; unmatched/ambiguous/reused reads make link/latency metrics unavailable.
- Exact operation names with at most one sanctioned prefix; no whitespace/case normalization.
- Distinct upstream source and domain-separated fact-set identities; fact set covers complete canonical emitted facts, is representation-order stable, and never supplies temporal precedence.
- C0/source feature wording only; no LoCoMo/MemGym activation, certification, promotion, or measurement-readiness claim.

## Verification

At integrated exact head run:

- `uv run pytest -q -o addopts='' tests/test_memory_continuity_producer.py tests/test_semantic_facts.py tests/test_feature_governance_control.py::test_feature_registry_zero_contract_errors`
- registry/CLI bootstrap regression sufficient to cover current canonical `8fa4d499`;
- touched `ty`, Ruff check/format, compile, and `git diff --check`;
- exact five-path diff/test preservation and clean status.

Commit cleanly and page `wH:p9` with old/new exact heads, commit/stat, counts, clean status, and no-root/no-activation/no-model confirmation.
