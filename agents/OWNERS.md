# Owners

Four stable lanes own every path and decision in this repository. These lanes
describe repository responsibilities, not Peter's persistent-chat identities,
a roster, or contact permissions. Missions are temporary and numbered
(`agents/missions/ACTIVE.md`); lanes are permanent.
A mission *leases* paths from a lane for its lifetime; the lane owner decides
semantics inside the lane. Disjoint leases are why parallel workers cannot
collide.

| Lane | Owns (paths) | Decides |
|---|---|---|
| **Integration** | `agents/` (this file, WORKFLOW, STRUCTURE, missions/, archive/, handoffs/), `.github/`, merge queue | Mission registration, cross-mission conflicts, merges, sunsets, lease grants |
| **Research** | `research/`, `digests/`, `docs/research/`, analysis sections of `sql/` | What counts as evidence, analysis semantics, findings, experiment agenda |
| **Tasks** | `library/` (benchmarks, curated, adapters, registry, synthetic staging) | Task admission, verification standards, benchmark pins, certification gates |
| **Platform** | `src/`, `tests/`, `scripts/`, `sql/` schema, `compose.yaml`, `pyproject.toml`, `uv.lock`, `Makefile`, `dashboard/` | Code architecture, CI/premerge contract, storage topology, tooling |

Docs follow their subject: `docs/research/` is Research; engineering and
operations docs are Platform; governance docs are Integration.

## The integrator

Exactly one session at a time acts as integrator (Integration lane). The integrator
edits `missions/ACTIVE.md`, coordinates overlapping integration branches, resolves
cross-mission conflicts, and sunsets spent branches/worktrees. Routine protected PR
merges are owned by the PR author; the integrator handles cross-mission integration merges
and conflict resolution. Workers who hit a conflict with another mission stop
only the contested files, record the dependency in their handoff, and continue
independent safe work — they never resolve it themselves.

## Peter's reserved authority

Peter alone decides, and is asked *only* about:

1. **Policy and spend** — `policy/standing-approvals.yaml` content, cost
   ceilings, anything billable or cloud (`escalate_to_human` classes).
2. **Publication** — anything leaving the repository: pushes to public repos,
   external PRs, publishing tasks or results. Normal pushes/PRs in Peter's
   authorized repository follow the task and repo contract without a second approval.
3. **Research direction** — which hypotheses the lab pursues; accepting or
   rejecting DISCOVERIES entries.
4. **Registration of evaluation tasks** — promotion into `registered/*`.

Everything else is a lane decision. Choose informed local implementation
decisions within the task; if a genuine unresolved scope/authority conflict
remains, state the two readings and ask one focused question.
