---
name: change-impact
description: Analyze affected consumers and safety invariants for an explicitly requested Python/schema/CLI/storage refactor or contract review.
---

# Change impact

State the changed contract and its one or two safety-critical invariants. Use LSP references for exported symbols and explicit searches for wire/CLI/SQL/config/task/verifier/dynamic consumers. Check pinned upstream behavior and introducing history only when the conclusion depends on them. An empty search clears only its searched surface.

Choose proof proportional to the actual risk: exact implementation/contract, reachability reasoning, focused behavioral test, or actual runtime path. Do not call compilation, an unrelated suite, or a worker's confidence behavioral proof. Mark unavailable invariants unproven. Read-only/shared-worktree delegates return evidence and a focused reproduction plan unless execution is explicitly assigned; the integration owner performs authorized checks at the barrier.

## Delegated change packet

Bind the resolved absolute checkout/cwd, owned files and non-goals, observable
acceptance, source pointers, and verification owner once. Never reconstruct a home
directory from a person's or role's name. Supply the contract and a few cited
starting ranges, not the parent's whole transcript; the worker still reads current
governing instructions and source needed for safe edits.

For isolated snapshots, name the interpreter/dependencies actually available and
whether Git metadata exists. In evaluations, runtime pointers must not reveal the
solution checkout. State whether the implementer or parent owns verification;
only explicitly edit-only workers skip checks. Start discovery in owned source
paths and named consumers, not bulk `library/` or calibration/evidence corpora;
expand into those when the assignment or dependency requires them.
Parent-owned verification still requires exercising the patched path and the
repository's integration gates before acceptance; delegation is not a waiver.

In OMP, route file reads/search/discovery/edits to `read`/`grep`/`glob`/`edit`,
not shell equivalents. A policy rejection is a routing correction, not a reason
to retry a differently quoted shell command. After an edit, both the tag and line
coordinates may change: use only ranges displayed in the new snapshot, or refresh
the next small range before patching. A new tag does not make old coordinates
visible. A `write` response without numbered source is not an edit snapshot.

## Python/Pydantic boundary cutover

Normalize one canonical value type at parse boundaries and reject invalid shapes explicitly. Verify serialization using `model_dump(mode="json")`; verify schema separately using `model_json_schema()` and real wire-contract tests. Keep meaningful security/CLI/persisted-format fixtures. Replace AST/source-layout tests only when behavioral coverage proves their actual contract. Use explicit tagged types for meaningful absence and migrate every known caller; do not add aliases/shims to avoid the cutover.

Return the changed contract, affected consumers, observed proof, concrete remaining risks, and cheapest reproduction that could falsify the conclusion. Keep the work scoped; not every tiny diff needs six ceremonial report sections.

## Provenance

Contract-impact method adapted from Lauren Tan's MIT-licensed Pstack blast-radius skill (`https://github.com/cursor/plugins/blob/main/pstack/skills/blast-radius/SKILL.md`); incorporates the existing domain-first boundary-cutover recipe with serialization/schema distinctions corrected.
