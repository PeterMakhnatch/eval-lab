---
name: repository-health
description: Audit and improve eval-lab repository health: local/CI parity, duplicated rules, stale generated state, mission and worktree hygiene, source-of-truth drift, slow maintenance commands, and recurring agent mistakes that should become deterministic checks.
---

# Eval-lab repository health

Reduce maintenance cost without creating another governance layer.

## Baseline first

1. Read the current mission, ownership, structure, and check contracts already supplied by the repository.
2. Record the exact branch and dirty state. Use `git worktree list --porcelain` for the worktree inventory; never infer paths from naming conventions.
3. Identify the authoritative source for the reported problem and every derived display or document.
4. Measure the same observable before and after: command latency, output size, stale entries, diagnostic count, worktree disk use, or failing gate.

## Route recurring failures to structure

When the same correction appears twice, prefer the strongest practical mechanism:

1. type or schema that makes the invalid state unrepresentable;
2. lint, test, governance check, or CI assertion;
3. canonical helper or idempotent command;
4. runtime boundary validation;
5. scoped skill or documentation only when judgment is required.

Delete superseded prose after deterministic enforcement lands. Do not add another instruction beside an existing authoritative rule.

## Check repository-specific drift

- **Local versus CI:** compare `scripts/premerge.sh`, `agents/CHECKS.md`, and `.github/workflows/`. Local green must not permit a state CI rejects.
- **Status truth:** trace fleet and mission displays back to `agents/missions/ACTIVE.md`, live handoffs, git state, and GitHub state. Fix the producer or contract rather than editing a derived report.
- **Worktrees:** classify dirty work as active until proven otherwise. Cross-check branch ancestry, mission ownership, pull-request state, handoff state, and recent activity. Never delete a dirty or in-use worktree without the user's explicit decision.
- **Generated state:** do not hand-edit `runs/`, `queue/`, `derived/`, backups, generated indexes, or immutable evidence. Fix the generator or regeneration path.
- **Instruction weight:** keep `AGENTS.md` for durable facts and hard boundaries. Put optional procedures in `.omp/skills/`; do not introduce sticky mode catalogs.
- **Root structure:** any new top-level entry must be registered in `agents/STRUCTURE.md` in the same change.

## Make and verify the change

1. Apply the smallest coherent fix at the authoritative surface.
2. Remove the obsolete duplicate or contradictory rule the fix supersedes.
3. Exercise the actual maintenance command or gate.
4. Run focused checks for the touched contract; use the repository's full premerge path only at its required checkpoint.
5. Report the baseline, observed result, files changed, held-back destructive actions, and remaining risk.

## Provenance

Adapted for OMP and eval-lab from two MIT-licensed Pstack mechanisms by Lauren Tan: `principle-encode-lessons-in-structure` and the safety gates in the `worktree-cleanup` playbook. No Cursor, Graphite, Bun, or TypeScript automation is imported.
