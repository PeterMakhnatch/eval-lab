# Mission board

The sole live board. Only the integrator edits this file. States:
`ready` → `active` → `review` → `merged`, with `blocked` possible anywhere.
Template: `TEMPLATE.md`. Finished missions move to `agents/archive/` with a
date prefix.

## Now

- **M001** (active → review at PR open): replace the codename role registry
  with lanes + numbered missions; make fleet-status truthful.
- **Legacy in-flight, pre-M001 registration** — adopted onto this board so it
  stays the single source of truth:
  - `role/program` (PROGRAM, Research): experiment-journal reconciliation;
    last commit ~2h before M001 registration.
  - `role/organization-prompts` and `role/wave4-prompts` (Integration):
    prompt-organization work, live writers at M001 registration.
  - `role/observatory-b3` worktree (Platform): observability follow-up;
    b/b2 branches look spent — integrator to confirm and sunset.

## Review

- Nothing open at M001 registration. M001's own PR joins this list when it
  opens and stays here until a non-author merges it.

## Next

- **M002 (ready, Platform):** lower the ty ratchet 33 → 28 in
  `typecheck.yml`/`premerge.sh` (premerge already prints the notice).
- **M003 (ready, Integration):** triage the spent-branch backlog
  (`role/data-strategy*`, `role/solidify*`) — confirm merged content, sunset
  branches; do not delete anything still carrying unmerged commits.

## Needs Peter

- Review and merge decision on M001's PR (`M001: simplify mission
  governance`). Nothing else currently requires Peter's reserved authority.

---

## Missions

| ID | Outcome | Lane | Agent/model | Worktree / branch | Exclusive paths (lease) | Deps | Acceptance | PR | State | Merge owner |
|---|---|---|---|---|---|---|---|---|---|---|
| M001 | Codename registry replaced by lanes + missions; fleet-status derives truthful state | Integration | Claude (Opus 5, interactive session) | `.worktrees/m001-governance` / `role/m001-governance` | `agents/{WORKFLOW,STRUCTURE,ROLES,OWNERS}.md`, `agents/missions/`, `agents/archive/`, `docs/operating-manual.md`, `scripts/fleet-status.sh`, `tests/test_fleet_status.py`, `.github/pull_request_template.md`, `agents/handoffs/m001-governance.md` | none | OWNERS 4 lanes; board sole source; archive preserves history; fleet-status hides squash-spent branches + flags unregistered/stale; injected-output tests green; premerge green; PR open, not self-merged | (opens with this change) | review | integrator (not the author) |
| LEGACY-PROGRAM | Truthful experiment ledger + agenda | Research | (pre-M001 prompt) | `.worktrees/program` / `role/program` | `research/experiments/`, `agents/handoffs/program.md` | none | per its original mission text | — | active | integrator |
| LEGACY-ORG-PROMPTS | Prompt organization | Integration | (pre-M001 prompt) | `.worktrees/organization-prompts` / `role/organization-prompts` | its worktree paths | none | per its original mission text | — | active | integrator |
| LEGACY-WAVE4 | Wave-4 prompt drafts | Integration | (pre-M001 prompt) | `.worktrees/wave4-prompts` / `role/wave4-prompts` | its worktree paths | none | per its original mission text | — | active | integrator |
| LEGACY-OBSERVATORY | Observability follow-up | Platform | (pre-M001 prompt) | `.worktrees/observatory` / `role/observatory-b3` | its worktree paths | none | per its original mission text | — | active | integrator |
