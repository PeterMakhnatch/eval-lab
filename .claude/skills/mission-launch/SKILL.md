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

Five parts, always: identity + worktree setup; exclusive owned paths;
the mission and acceptance; boundaries; handoff discipline
(`agents/handoffs/<role>.md`). Copy the relevant workstream section from
`docs/build-plan.md` when the mission is a WS item.

## 3. Worktree and branch

Primary checkout is read-only for this launch. Work only under
`.worktrees/`:

```bash
cd ~/Developer/eval-lab
git fetch origin
git worktree add .worktrees/<role> -b role/<role> origin/main
cd .worktrees/<role>
uv sync
```

Lease: one writer per worktree. Do not edit `~/Developer/eval-lab`
itself. Stage explicit paths only — never `git add -A`. Never commit
`research/lessons.md` or `digests/DISCOVERIES.md`.

## 4. Board entry

Only the integrator edits `agents/missions/ACTIVE.md`. Use
`agents/missions/TEMPLATE.md`. States: `ready` → `active` → `review` →
`merged`. The worker owns the lease and the handoff; the board row is
integrator work. If you are not the integrator, write the proposed row
in the handoff and stop.

Handoff first four lines, parsed by `scripts/fleet-status.sh`:

```
Status: building | blocked | review-wanted | done
Last: <one line>
Next: <one line>
Blockers: <one line or none>
```
