# Role registry

Who exists, what they own, where they stand. New role = new row, by PR.
Status column is updated by the role itself or the integrator.

| Role | Branch | Owns (exclusive write) | Mission | Status (2026-08-13) |
|---|---|---|---|---|
| BUILDER | `main` | `src/`, `tests/`, `sql/`, `docs/prompts/`, `docs/`, `scripts/`, `compose.yaml`, `pyproject.toml`, `uv.lock`, `Makefile` | Briefs from `docs/design-additions.md` + `docs/fleet-tracking.md` | Briefs 05–07 merged (executor, nightly digest, canaries). Next: brief 08 (Phoenix) or 12 (fleet reporting). |
| CURATOR | `role/curator` | `library/curated/`, `agents/handoffs/curator.md` | Verified library of 15–25 open-source Harbor tasks with provenance/verification cards | 17-task library merged; verification runs still in progress in its worktree. |
| ADAPTER | `role/adapter` | `library/adapters/`, `agents/handoffs/adapter.md` | One external benchmark adapted end-to-end (QuixBugs) | QuixBugs adapter + generated tasks + verification evidence merged. Mission complete pending review. |
| EVIDENCE | `role/evidence` | `research/calibration/`, `agents/handoffs/evidence.md` | Judge-calibration corpus + failure-taxonomy trajectory labels | Corpus + trajectory labels merged. Mission complete pending review. |
| RECON | `role/recon` | `research/explorations/`, `agents/handoffs/recon.md` | Working demos + adoption notes for unused Harbor 0.21 capabilities | Capability map + demos merged; self-reported complete. |

Peter owns `policy/standing-approvals.yaml` content (agents ship conservative
defaults, never loosen).

## Worktree locations

All worktrees live in `.worktrees/<role>` inside the repo (see
`agents/WORKFLOW.md`). Exception being wound down: `role/curator`'s worktree
is still at the legacy `../helab-curator` path because its verification
session is active; when it stops, the integrator runs:

```bash
git worktree move ../helab-curator .worktrees/curator
```
