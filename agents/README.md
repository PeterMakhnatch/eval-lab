# Agent coordination and governance

## Purpose
`agents/` defines coordination protocols, ownership boundaries, and verification
standards for agents and humans collaborating in eval-lab. It establishes how
work is planned, executed in isolated worktrees, handed off, and verified.

## What lives here / entry points
- `agents/WORKFLOW.md`: Git worktree procedures, branch rules, and PR boundaries.
- `agents/OWNERS.md`: Four stable ownership lanes and Peter's reserved authority.
- `agents/CHECKS.md`: Definition of Green, quality gates, and verification contract.
- `agents/STRUCTURE.md`: Binding repository layout and frozen root map.
- `agents/missions/ACTIVE.md`: Active mission board and current gates.
- `agents/missions/TEMPLATE.md`: Template for declaring new mission scopes.
- `agents/handoffs/`: Active mission status files (one per live mission).
- `agents/archive/`: Dated records of completed missions and handoffs.

## Invariants or rules
- Root freeze authority: `agents/STRUCTURE.md` is the single source of truth for allowed root entries (cited in `agents/STRUCTURE.md`).
- Live-only handoffs: `agents/handoffs/` holds active missions only; finished ones move to `agents/archive/` (cited in `agents/STRUCTURE.md`).
- Immutable archives: After archival, handoff records are never edited or deleted (cited in `agents/STRUCTURE.md`).
- Safe execution: All work happens in isolated worktrees, never directly on main (cited in `agents/WORKFLOW.md` and `AGENTS.md`).

## Tests or checks
- `python -m evallab.governance check`
- `uv run pytest tests/test_governance.py`

## What not to add here
- Do not store research findings, benchmark definitions, or application code here.
- Do not add finished handoff files here without moving them to `agents/archive/`.
- Do not duplicate rules from `AGENTS.md` or create unapproved coordination frameworks.
