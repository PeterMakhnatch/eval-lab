---
name: mission-launch
description: Set up a substantial authorized Eval Lab mission with a scoped context pack and isolated worktree; reuse the assigned queue/handoff and skip extra ceremony for small chat tasks.
---

# Mission launch

No Harbor runs. No paid models. One writer per worktree.

## 1. Context pack

`evallab context` is not a CLI command. The pack compiler is:

```bash
uv run python -m evallab.contextpack build <mission_type> [--path REL] [-o out.md] [--task REF]
```

`mission_type` is one of `builder`, `analyst`, `runner`, `operator`.
Pass repeatable `--path` for the files or directories the mission will touch
to scope the pack to relevant living docs. Without `--path`, the compiler
selects docs by mission audience for general orientation.
Two consecutive builds of the same tree must be byte-identical. Point
the brief at the pack path; do not paste a docs crawl.

Build a context pack only when the mission needs one; a small change already
specified by Peter needs no extra scaffolding.

## 2. Brief

For a substantial mission, state identity and worktree, owned paths, acceptance,
boundaries, and the handoff plan. Use `agents/missions/TEMPLATE.md` when useful;
do not create a separate brief for a small change already specified by Peter.

## 3. Worktree and branch

Follow `agents/WORKFLOW.md` for native worktree creation and environment setup.
The primary checkout remains on `main`; work is isolated under `.worktrees/`
or `/private/tmp/`. Stage only intended paths. Evidence generation and publication
require their own task scope; launching a mission does not authorize either.

## 4. Queue

Use the current assignment: the Linear lane (`lin next`) or Peter's direct
request, which is authorization for scoped free work. The historical board
(`research/inbox/board.md`) preserves past pickup records; it is not the
default backlog for new work. `agents/missions/ACTIVE.md` is navigation only.

For durable work with a live handoff, link it from the existing Linear issue.
Its four-line header and closure/archive procedure are defined in
`agents/WORKFLOW.md`. Use the actual PR, exact head, and command evidence to
distinguish implementation, review readiness, and merge; a handoff does not
substitute for CI.
