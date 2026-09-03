# Board — pull, don't push

Seeded 2026-09-03 from memory of in-flight work. Peter: correct backlog order,
add/drop items. Agents: edit ONLY `claims` (your own line) and propose backlog
items. Backlog order and bars are Peter's.

# house rules
1. No agent assigns work to another agent. Propose it to the lane backlog
   instead (`proposed by <id>: ...`). No DM tasking, ever.
2. Only 🟣 Peter pushes. Everyone else pulls at claim rounds.
3. 🎯 tab = heads down, do not page (backlog it instead). 📥 tab = open.
4. Urgency goes through Peter, never peer-to-peer.
5. One claim at a time per agent. Finish or explicitly release before claiming
   again. Stale 24h with nothing posted → task returns to the board.
6. Passing is allowed and neutral. A pass with a reason is data.
## good-codebase (bar: CI green + focused tests for touched path)
backlog:
  1. runner retry semantics cleanup
  2. digest helper dedup
claims:
done:

## analysis (bar: every claim cites artifact digest + file:line)
backlog:
  1. PROBE: why did lane-3 evals flake on Sep 2? (small, calibration task)
claims:
done:

## system-design (bar: decision + rejected alternatives + falsifiable kill gate)
backlog:
  1. PROBE: verl second-wave criteria — when, if ever, do we reopen it?
     (small, calibration task; prior: REJECT for v1 per Track F scorecard)
  2. S0 GPU execution class sign-off (24GB CUDA declared; needs owner confirm)
claims:
done:

## tooling (bar: documented in-repo, focused test, no new harness dependency)
backlog:
  1. (Peter to seed: eval-lab runner/scripts/tooling needs)
claims:
done:

## omp-usability (bar: Peter can operate it half-asleep; documented in 5 lines)
backlog:
  1. (Peter to seed: harness workflow gripes — paging, tabs, board ops)
claims:
done:

## training-signal (bar: digest-bound manifest, no held-out claims by trainer)
backlog:
  1. S0 dry-plan fixtures validate (Qwen3-0.6B template render, no GPU)
claims:
done:

# ledger (agent x lane — updated at review time; ✓/~/✗)
(none yet — fills from calibration round)
