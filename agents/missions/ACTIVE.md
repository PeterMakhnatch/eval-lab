# Mission board

The sole live board. Only the integrator edits this file. States:
`ready` -> `active` -> `review` -> `merged`, with `blocked` possible anywhere.
Template: `TEMPLATE.md`. Finished missions move to `agents/archive/` with a
date prefix.

## Now

- **M002 (Platform):** prove and expose the existing operational analysis
  loop. The worker reports local acceptance green and is rebasing/opening its
  PR; until that PR exists, the mission remains active.

## Review

- **M001 (Integration), PR #41:** governance reset; exact-head checks were
  green before this integrator board correction and must rerun successfully.
- **A001 (Integration audit), PR #40:** residual branch/worktree report only;
  no cleanup was executed. Exact-head checks are green.

## Next

- **M003 (ready, Platform):** provider-neutral agent/profile and subscription
  authentication contract. It may start after M001 merges; its core lease is
  disjoint from M002 and excludes CLI/status/dashboard wiring.
- **M004 (candidate, report-only):** evaluate a generated agent-wiki pilot
  after A001 closes. No package installation, credential change, generated-doc
  commit, or CI integration is authorized by this candidate entry.

## Needs Peter

- Nothing. Current merge, cleanup, and mission-sequencing decisions belong to
  the integrator. Peter's reserved decisions remain policy/spend, publication,
  research direction, and task registration.

---

## Missions

| ID | Outcome | Lane | Agent/model | Worktree / branch | Exclusive paths (lease) | Deps | Acceptance | PR | State | Merge owner |
|---|---|---|---|---|---|---|---|---|---|---|
| M001 | Codename registry replaced by lanes + missions; fleet-status derives truthful state | Integration | Claude (Opus 5, interactive session) | `.worktrees/m001-governance` / `role/m001-governance` | `agents/{WORKFLOW,STRUCTURE,ROLES,OWNERS}.md`, `agents/missions/`, `agents/archive/`, `docs/operating-manual.md`, `scripts/fleet-status.sh`, `tests/test_fleet_status.py`, `.github/pull_request_template.md`, `agents/handoffs/m001-governance.md` | none | OWNERS 4 lanes; board sole source; archive preserves history; fleet-status hides squash-spent branches + flags unregistered/stale; injected-output tests and premerge green; PR exact-head checks green | #41 | review | integrator (not the author) |
| M002 | Existing Harbor completion path exposed as one deterministic analysis/status/dashboard slice | Platform | pre-M001 dispatch; exact agent/model pending handoff | `.worktrees/m002-operability` / `role/m002-operability` | `src/evallab/{smoke,status}.py`, `tests/test_{smoke,status}.py`, `tests/fixtures/operability/`, `dashboard/`, `docs/operator-demo.md`, `agents/handoffs/m002-operability.md`, minimal additive `src/evallab/cli.py` | none | smoke x3; experiment/job/trial/trajectory/analysis join; truthful cold-start status; CLI/dashboard parity; pytest/Ruff/premerge; PR exact-head checks | — | active | integrator |
| A001 | Every residual worktree/branch classified with evidence; no cleanup executed | Integration | pre-M001 dispatch; exact agent/model pending handoff | `.worktrees/a001-state-audit` / `role/a001-state-audit` | `research/maintenance/active-state-audit-2026-08-15.md`, `agents/handoffs/a001-state-audit.md` | none | per-ref evidence and disposition; two classifications spot-checked; report-only PR green | #40 | review | integrator |
| M003 | Hard-coded agent/auth assumptions replaced by immutable provider-neutral profiles without an API-key path or live call | Platform | unassigned | `.worktrees/m003-profiles` / `role/m003-profiles` | `src/evallab/profiles.py`, `src/evallab/credentials.py`, `src/evallab/runner.py`, `tests/test_profiles.py`, `docs/agent-profiles.md`, `agents/handoffs/m003-profiles.md`; no CLI/status/dashboard | M001 | declared/installed/credential/smoke/canary states separate; subscription-only probes; injected deterministic tests; pytest/Ruff/premerge and exact-head CI | — | ready | integrator |
