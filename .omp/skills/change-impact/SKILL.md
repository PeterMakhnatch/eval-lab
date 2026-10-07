---
name: change-impact
description: Analyze affected consumers and safety invariants for an explicitly requested Python/schema/CLI/storage refactor or contract review.
---

# Change impact

State the changed contract and its one or two safety-critical invariants. Use LSP references for exported symbols and explicit searches for wire/CLI/SQL/config/task/verifier/dynamic consumers. Check pinned upstream behavior and introducing history only when the conclusion depends on them. An empty search clears only its searched surface.

Choose proof proportional to the actual risk: exact implementation/contract, reachability reasoning, focused behavioral test, or actual runtime path. Do not call compilation, an unrelated suite, or a worker's confidence behavioral proof. Mark unavailable invariants unproven. Read-only/shared-worktree delegates return evidence and a focused reproduction plan unless execution is explicitly assigned; the integration owner performs authorized checks at the barrier.

## Python/Pydantic boundary cutover

Normalize one canonical value type at parse boundaries and reject invalid shapes explicitly. Verify serialization using `model_dump(mode="json")`; verify schema separately using `model_json_schema()` and real wire-contract tests. Keep meaningful security/CLI/persisted-format fixtures. Replace AST/source-layout tests only when behavioral coverage proves their actual contract. Use explicit tagged types for meaningful absence and migrate every known caller; do not add aliases/shims to avoid the cutover.

Return the changed contract, affected consumers, observed proof, concrete remaining risks, and cheapest reproduction that could falsify the conclusion. Keep the work scoped; not every tiny diff needs six ceremonial report sections.

## Provenance

Contract-impact method adapted from Lauren Tan's MIT-licensed Pstack blast-radius skill (`https://github.com/cursor/plugins/blob/main/pstack/skills/blast-radius/SKILL.md`); incorporates the existing domain-first boundary-cutover recipe with serialization/schema distinctions corrected.
