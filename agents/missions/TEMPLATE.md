# Mission template

Copy the row into `ACTIVE.md` (integrator only). Keep every field honest at
registration time — the board is the single source of truth, and a stale
board is worse than no board.

| Field | Content |
|---|---|
| ID | `M###`, next unused number |
| Outcome | one sentence, testable, past-tense ("X replaced by Y") |
| Lane | Integration / Research / Tasks / Platform (`OWNERS.md`) |
| Agent/model | who executes, precisely (harness + model) |
| Worktree / branch | `.worktrees/m###-slug` / `role/m###-slug` |
| Exclusive paths | the lease — every path this mission may write; disjoint from other active missions |
| Deps | mission IDs that must merge first, or `none` |
| Acceptance | the checkable list that defines done |
| PR | number once open |
| State | ready / active / review / blocked / merged |
| Merge owner | who merges — never the mission author |

Worker rules (mirror of `WORKFLOW.md`): work only inside the lease; stop and
record on any conflict; handoff at `agents/handoffs/m###-slug.md` with the
4-line header; premerge before push; the PR title starts `M###:`.
