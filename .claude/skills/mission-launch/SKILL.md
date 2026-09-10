---
name: mission-launch
description: >
  Assemble an eval-lab mission: compile the context pack, write the
  brief, create the worktree and branch, and record the board entry.
  Use when launching or scaffolding a mission, opening a role worktree,
  or Peter asks to start work without colliding with the primary checkout.
---

# Mission launch

No Harbor runs. No paid models. One writer per worktree.

## 1. Context pack

`evallab context` is not a CLI command. The pack compiler is:

```bash
uv run python -m evallab.contextpack build <mission_type> [-o out.md] [--task REF]
```

`mission_type` is one of `builder`, `analyst`, `runner`, `operator`.
Two consecutive builds of the same tree must be byte-identical. Point
the brief at the pack path; do not paste a docs crawl.

## 2. Brief

For a substantial mission, state identity and worktree, owned paths, acceptance,
boundaries, and the handoff plan. Use `agents/missions/TEMPLATE.md` when useful;
do not create a separate brief for a small change already specified by Peter.

## 3. Worktree and branch

Follow `agents/WORKFLOW.md` for native worktree creation and environment setup.
The primary checkout remains on `main`; work is isolated under `.worktrees/`
or `/private/tmp/`. Stage only intended paths. Evidence generation and publication
require their own task scope; launching a mission does not authorize either.

## 4. Claim

Use `research/inbox/board.md` for backlog order and the pull protocol, and
`research/inbox/claims/README.md` for the existing claim-file format. Sign
pane/session plus model; write one open claim file, not a competing roster or
peer assignment. `agents/missions/ACTIVE.md` is navigation only.

For durable work with a live handoff, link it from the claim. Its four-line
header and closure/archive procedure are defined in `agents/WORKFLOW.md`.
Use the actual PR, exact head, and command evidence to distinguish implementation,
review readiness, and merge; a claim or handoff does not substitute for CI.
