# TB4 QA agent v1: does fixing the harness beat a plain read on the same model?

Bridge: `research-context/bridge/2026-10-20-open-qa/qa-agent-v1.md`. Key: `tb4-audit-v1`, TEST split
(41 tasks, 152 task findings). Scorer: `research/answer-keys/tb4-audit-v1/score_audit.py`, unchanged.

## What changed from v0 (#1117, agent `22eac188`, campaign `qa2-frozen-test-20261020-01`)

Only what the v0 diagnosis supports (all 41 v0 sessions hit the 8-tool cap; 64/328 calls failed on
the 200-line read limit; at most one claim per task):

| | v0 | v1 |
|---|---|---|
| Tool calls per task | 8 | 60 |
| Wall clock per task | 120 s | 900 s |
| `task-info` read window | default 100 lines, refused above 200 | default and maximum 2,000 lines; a longer range is clipped with a continuation note |
| Findings per task | not stated; cap left room for one | "record each distinct defect as its own finding" |
| Tool surface | 7 JSON tools; the 4 runtime tools could not run (no Docker admission) | the 3 source tools: `task-info`, `diff`, `record-finding` |
| Model / route | Codex `gpt-6.1-sol`, JSON-action loop | `anthropic/claude-opus-5-5`, native OMP subagents (same family as the plain read, #1119) |

Kept: the packaged common rules and eleven class playbooks (verbatim), the evidence and citation rules,
`EnvTools` bound to the sealed TEST view exactly as v0 bound it, citation validation in
`record-finding`, source-only `not_run`, no self-verification. Nothing was taken from TEST labels.

`src/evallab/qa/ask.py`, `env.py` and `env_agent.py` carry the same read-window, observation-size and
multiple-findings fixes for the `evallab qa env audit` loop. No v1 TEST session ran through that loop.

## Route

The Claude Code subscription lane (#862/#870/#874) runs Claude Code as a Harbor solver agent inside a
sandbox (`evallab tasks prepare --agent claude-code`, queue/approve/tick), and its auth-gateway bridge
only serves that sandbox's `/v1/messages`. The v0 harness's only model transport is `CodexSession`.
So that lane cannot drive this host-side harness without a new transport, and v1 ran as **native OMP
subagents**: one fresh `anthropic/claude-opus-5-5` subagent per task, at most 3 at once. Each got the
rendered `prompt_v1.md` as its whole assignment and four kernel-hosted tools (`omp_driver.py`) that
forward to `harness.py serve`. The harness binds each task under an opaque session id and enforces
the call and time budgets.

## Files

- `harness.py`: tool host (`serve`) and prompt renderer (`prompt --session`).
- `prompt_v1.md`: frozen prompt template.
- `omp_driver.py`: the OMP eval-kernel tools and dispatcher.
- `score.py`: scores v0, the static scanner (#1118, rerun; deterministic), the plain read (#1119
  static arm) and v1 through the key scorer. Run from repo root:
  `.venv/bin/python research/experiments/tb4-qa-agent-v1-20261020/score.py --campaign test`.
- `results-test.json`, `results-dev-smoke.json`: scored tables. `sessions-test.json`: per-session
  budgets, findings, transcript tool audit and tokens. `spotcheck-test.json`: 10 unmatched v1 findings
  read by a checker.
- Raw runs (journals, findings, rendered prompts): `~/Developer/eval-lab-results/2026-10-20/tb4-qa-agent-v1/{dev-smoke,test}/`.

## Freeze

Frozen commit `327d27495b7889b7360658afc8ec30c1b01cd03c` (06:27:12 ET). The first TEST bind was at
06:27:36 ET. sha256:

- `prompt_v1.md` `8262bb6cc83b74e48eb0fbcf3b56da0a847e7ad3677114d6fd55b2f02f358a0f`
- `harness.py` `f59439429a2eef6f2d1e72078ec63a7c7846fa836ac09e4ff3a77c72b96f4451`
- `omp_driver.py` `9b30973e44945e9bbe9a4e70da77c8814026c89b0f110fe45c25816c0a450f45`
- `src/evallab/qa/env.py` `3bf0769e37f6d7d7497fe5f29ceb06c2cc4be1fb3887a20f0d14b15046e434d3`
- `src/evallab/qa/ask.py` `24d77e7c8fe85c617f5d0cae7b9713b32c26e5d2abde88aa387834cfe835a8a2`

After the freeze, only `score.py` changed: its static-scanner output loader. No v1 row was affected.

## Result (TEST, 152 findings; category 1-to-1 / strict `locus_and_class` / task-level `task_any_1to1`)

| Method | Category | Strict | Task-level | TEST claims | Without biped (of 144) |
|---|---|---|---|---|---|
| QA agent v0 (Codex) | 16 | 1 | 27 | 29 | 15 / 1 / 26 |
| Static scanner | 46 | 1 | 75 | 112 | 43 / 1 / 72 |
| Plain Opus read | 78 | 4 | 115 | 151 | 73 / 4 / 108 |
| **QA agent v1 (Opus)** | **80** | **3** | **116** | **148** | **76 / 3 / 111** |

Paired bootstrap over tasks (10,000 resamples, seed 20261020), v1 minus plain read: category +1.3 pts
[−6.5, +8.8]; task-level +0.7 [−9.5, +11.0]; strict −0.7 [−2.5, 0.0]. **No detectable difference.**
Caveats and the spot check are in the bridge note.
