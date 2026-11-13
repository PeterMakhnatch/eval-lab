---
status: living
audience: [builder, analyst, runner, operator]
---

# Start here

Eval Lab finds and measures reward hacking: agents that pass a task by cheating.
The centre of the lab is labelled data where the cheating step is pinpointed.
A verifier reward does not prove an honest solve.

## Product entry commands

| Function | Purpose | Entry command |
|---|---|---|
| run | Execute a task and keep its evidence | `uv run evallab run --help` |
| gold | Read the labelled trace registry | `uv run evallab failures bank gold-counts --json` |
| detect | Judge a finished run | `uv run evallab line --help` |
| watch | Flag a run while it happens | `uv run evallab watch --help` |
| certify | Check a task and its grader | `uv run evallab certify-gold --help` |
| score | Measure the 4 QA contracts below | `uv run evallab score --help` |
| doctor | Check tools, data, and operations | `uv run evallab doctor` |

## Gold today
Use the public `evallab.gold.load_rows` and `load_trajectory` APIs.
Read current registry counts with the Gold command above, not a copied table.
`docs/score.md` owns frozen inputs, score reproduction, and publication.

## QA measures and limits

The 4 QA measures cover offline detection, online detection, task QA, and grader QA.
Their current values and coverage are in `~/Developer/eval-lab-results/score/current/scoreboard.json` and its `SCOREBOARD.md`.
[Score](score.md) gives the reproduction command and explains the separate denominators. No pooled accuracy is available.
No untouched test cohort exists: every Goodfire DEV run has stored detector outputs.
Online model monitors have no stored outputs on Gold prefixes. Native deterministic replay is available.
The 2 certifier generations are not merged because the strict study's frozen manifest is lost.
Restore removed code from `pre-deslop-2026-11-10`; [research/ARCHIVE.md](../research/ARCHIVE.md) maps the removals.

## Manuals and delivery

For a code change, follow the ordered path and domain router in `AGENTS.md`.
For product use, select the matching function above.
`docs/SYSTEM.md` owns architecture and evidence boundaries.
A merge does not authorize execution, deployment, or spend.

