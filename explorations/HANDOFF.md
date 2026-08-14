# RECON handoff

Updated: 2026-08-13

## Goal

Map unused Harbor 0.21 capabilities: one-page notes + tiny free/local demos
under `explorations/harbor-021/`. Rank adoption in `explorations/EXPLORATIONS.md`.

## What changed

- Worktree `~/Developer/helab-recon` on `role/recon` from `origin/main`.
- Nine notes in `explorations/harbor-021/` covering check, analyze,
  `--plugin`, exec, hub+dataset, multi-step, allowlist, separate verifier,
  atif2otel. Ranking in `explorations/EXPLORATIONS.md`.
- Demos under `explorations/harbor-021/demos/`; captures under
  `explorations/harbor-021/captures/`.
- Real Codex ATIF fixture at `explorations/harbor-021/fixtures/trajectory.json`
  (copied from harbor-practice; also at `runs/atif-source-trial/`).

## How it was verified

| Demo | Result |
|---|---|
| `run-check.sh` | 11-criterion rubric; shipped validator exit 0 on seeded result, 1 on incomplete |
| `run-analyze.sh` | `reward_hacking,task_specification`; validator exit 0 / 1 as above |
| `run-plugin.sh` | oracle reward 1.0; hooks.jsonl has start + trial + end |
| `run-exec.sh` | compiled task; oracle mean 1.000 |
| `run-hub.sh` | local `dataset.toml` + add event-summary; no publish |
| `run-multistep.sh` | both steps reward 1.0; rollup 1.0 |
| `run-allowlist.sh` | `ValueError: network_mode='allowlist' is not supported by EnvironmentType.DOCKER` |
| `run-separate-verifier.sh` | `verifier_environment_mode = "separate"`; reward 1.0 |
| `run-atif2otel.sh` | 0 issues; 10 spans; root AGENT `codex`; 24159-byte OTel JSON |

No compose, no Hub publish, no paid model.

## Next step

Done for this mission. Commit `111ce67` is on `role/recon` (explorations/
only). Do not open/merge the PR (out of scope). BUILDER can take
atif2otel + plugin into briefs 08/05.

## Blockers

None. Allowlist is unusable on Docker Desktop — recorded, not blocking.

## Do not

- Edit anything outside `explorations/`.
- Publish to Hub, start compose, or invoke a billable agent.
