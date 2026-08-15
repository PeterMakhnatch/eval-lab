# Owners

Four stable lanes own every path and decision in this repository. Missions are
temporary and numbered (`agents/missions/ACTIVE.md`); lanes are permanent.
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

Exactly one session at a time acts as integrator (Integration lane). Only the
integrator: edits `missions/ACTIVE.md`, resolves cross-mission conflicts,
merges PRs, and sunsets spent branches/worktrees. Workers who hit a conflict
with another mission **stop and record it in their handoff** — they never
resolve it themselves.

## Peter's reserved authority

Peter alone decides, and is asked *only* about:

1. **Policy and spend** — `policy/standing-approvals.yaml` content, cost
   ceilings, anything billable or cloud (`escalate_to_human` classes).
2. **Publication** — anything leaving the repository: pushes to public repos,
   external PRs, publishing tasks or results.
3. **Research direction** — which hypotheses the lab pursues; accepting or
   rejecting DISCOVERIES entries.
4. **Registration of evaluation tasks** — promotion into `registered/*`.

Everything else is a lane decision. Asking Peter a lane question is a
governance bug; deciding a Peter question in a lane is a policy violation.
