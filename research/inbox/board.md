# Board — pull, don't push

# What lives where (one line each)
# - problems: raw shared pains, anyone appends. Peter promotes to backlog.
# - backlog: the work menu. Peter orders. Agents pull top-down or pass.
# - claims: who is doing what RIGHT NOW (pane + model, not role names).
# - done: finished with attribution. ledger: the permanent record + marks.

and bars. Agents own only their `claims` line and may append `proposed by`
lines to backlogs. Ledger: research/inbox/ledger.md.

# house rules
1. No agent assigns work to another agent. Propose it to the lane backlog
   instead (`proposed by <pane> (<model>): ...`). No DM tasking, ever.
2. Only 🟣 Peter pushes. Everyone else pulls at claim rounds.
3. Claims identify pane AND model (e.g. `wH:p1 (Muse) → ...`), never a bare
   role name. "Who is the platform?" must be answerable from this file.
4. Urgency goes through Peter, never peer-to-peer.
5. One claim at a time per agent. Finish or explicitly release before claiming
   again. Stale 24h with nothing posted → task returns to the board.
6. Passing is allowed and neutral. A pass with a reason is data.
7. Paged on: claim rounds, completions needing Peter's review, urgency.
   Never paged on: proposals, picks, intermediate chatter (read the board).
8. New interaction rules start as problems below, graduate to house rules
   only after Peter promotes them. Rules emerge from pains, not vice versa.

# problems (shared pain ledger — anyone appends, Peter promotes to rules)
- P1 (Peter, Sep 3): agents page other agents out of the blue; OMP becoming
  unusable. Why they do it under investigation (prime suspect: peer-first
  delegation defaults in agent system prompts).
- P2 (Peter, Sep 3): research/inbox unusable mess — 180+ files, no index.
  (→ backlog item omp-usability #1 below.)
- P3 (Peter, Sep 3): role names (platform, analyst) hide WHO — pane/model
  identity missing. (→ rule 3 above; ledger format updated to match.)

## good-codebase (bar: CI green + focused tests for touched path)
backlog:
  1. immutable_directory nofollow hardening (from wH:p1 hand-off: atomic_no_replace_rename + queue.py caller to retained-parent-fd boundary + swap regressions)
  2. executor failure hygiene in queue.py (from engineer-lead hand-off: orphaned running/ reconcile, spec_id dupes, FileExistsError → archive-aside)
claims:
done:

## analysis (bar: every claim cites artifact digest + file:line)
backlog:
  1. reward_info projection gap (from wK:p7 hand-off: reward_components table, 1/0/5 split on tau archives — full acceptance bar in lead-sync-reply-wK-p7.md)
  2. PROBE: why did lane-3 evals flake on Sep 2? (small, calibration task)
claims:
  - wK:p7/Fable → calibration-tier deficit census over 130 action-memory + 6 tau archives (blocked: needs Peter yes/no on head fbee62dc + calibration-tier confirm)
done:

## system-design (bar: decision + rejected alternatives + falsifiable kill gate)
backlog:
  1. PROBE: verl second-wave criteria — when, if ever, do we reopen it? (prior: REJECT for v1 per Track F scorecard)
  2. S0 GPU execution class sign-off (24GB CUDA declared; needs owner confirm)
  3. evidence-visibility reconciliation (from wK:p4 hand-off: lessons dashboard says 0, ledger says 235 — join/filter defect, full contract in lead-sync-reply-wK-p4.md)
claims:
  - wK:p4/Tutor → Track F gates exercise (blocked: needs Peter on Linux host + ICC pilot; needs wH:p9 on SFT control arm)
done:

## tooling (bar: documented in-repo, focused test, no new harness dependency)
backlog:
  1. (Peter to seed)
claims:
  - engineer-lead → zai-opencode metered lane live-fire (k1h dispatched, watching SIGTERM cause)
done:

## omp-usability (bar: Peter can operate it half-asleep; documented in 5 lines)
backlog:
  1. inbox triage (from P2): 180+ files in research/inbox, unusable. Sort into
     live (board/ledger/queue/active briefs) vs archive (dated receipts) vs
     index. Concrete: move pre-Sep receipts to research/archive/<week>/, keep
     inbox to active + index file. No content changes, moves only, post the
     move list for review. Claimable by any agent with read-only tools + mv.
  2. (Peter to seed further gripes)
claims:
done:

## training-signal (bar: digest-bound manifest, no held-out claims by trainer)
backlog:
  1. Track C × Track H consumer reconciliation (from wS:p2 hand-off: SyntheticTaskCandidate rehydration + CAS quarantine binding, zero-edit H consume)
  2. S0 dry-plan fixtures validate (Qwen3-0.6B template render, no GPU)
claims:
  - wS:p2/Synth → Track C PR #360 repair (BLOCKED by p7 review; in prep against 12-item checklist)
done:
