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
| INGEST | `role/ingest` | `library/benchmarks/`, `agents/handoffs/ingest.md` | Pin and materialize 2026-cite public benches (survey + ≥4 Hub slices) | 16 surveyed; 4 Hub pins materialized + 19-task oracle/nop sample (2026-08-14). |

| INGEST | `role/ingest` | `library/benchmarks/`, `agents/handoffs/ingest.md` | Frontier benchmark survey + Harbor-runnable materialization | Registered 2026-08-14; mission in docs/prompts/overnight-missions.md |
| OBSERVER | `role/observer` | `src/harbor_lab/tracing.py`, `agents/handoffs/observer.md` (+additive compose/cli/pyproject) | Phoenix + ATIF trace shipping + OpenInference (brief 08) | Registered 2026-08-14 |
| ANALYST | `role/analyst` | `src/harbor_lab/{atif,facts,cohort}.py`, `research/analysis/`, `agents/handoffs/analyst.md` | ATIF→Parquet, deterministic facts, cohort compare, analysis sidecars (briefs 01–03) | Registered 2026-08-14 |
| RUNNER | `role/runner` | `research/experiments/`, `agents/handoffs/runner.md` | Design/submit/interpret real experiments via the queue | Registered 2026-08-14 |
| AUTOPILOT | `role/autopilot` | `src/harbor_lab/researchers.py`, `digests/DISCOVERIES.md`, `agents/handoffs/autopilot.md` | Bounded 24/7 researcher loop + discovery journal | Registered 2026-08-14 |
| FORGE | `role/forge` | `.github/`, `docs/engineering.md`, `agents/handoffs/forge.md` | Measured performance + CI/type hardening; held PRs for refactors | Done: PR #6 merged after its rebased GitHub checks were fully green. |
| JUDGE | `role/judge` | `src/harbor_lab/calibrate.py`, `research/calibration/records/`, `agents/handoffs/judge.md` | Judge calibration + DSPy experiment 1 (brief 09) | Registered 2026-08-14 |
| MEDIC | `role/medic-closeout` | CI compatibility, deterministic canary test, premerge gate, and green-check governance (one mission) | Make local and GitHub green agree; land PR #6 | Done: PRs #7 and #6 merged green; PR #8 closes documentation and branch-lifecycle governance. |


**Wave 1 outcome (2026-08-14):** INGEST, OBSERVER, ANALYST, RUNNER, AUTOPILOT,
JUDGE all merged into `main` (PRs #2–5 plus two local branches integrated).
FORGE landed through PR #6 after MEDIC restored green CI. Details per role in
`agents/handoffs/`; first
measured result: codex-as-judge calibration lands below the 0.90 floor.

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
