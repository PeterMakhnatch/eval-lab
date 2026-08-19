# Problem ledger — named problems, prior art, verdicts

The structured alternative to collecting random ideas. One row per named
engineering problem this lab must solve. The RESEARCHER mission
(docs/prompts/researcher.md) works one problem at a time: verify the
prior-art candidates actually exist, read them, write a short brief with
an adopt / adapt / build-ourselves / park recommendation. Peter approves
verdicts; approved rows become board missions. Plain language only.

Column meaning: WHAT WE HAVE = the lab's current answer, honestly stated.
CANDIDATES = prior art to check (UNVERIFIED = surfaced by an AI chat/search
and not yet confirmed against the actual paper/repo — verify before
believing).

## P1 — Step-level progress detection ("state-diff tracker")
- Problem: know whether an agent step actually changed the environment
  or was a no-op (reading, looping, flailing). ATIF records the agent's
  words, not the world's changes.
- What we have: nothing per-step. Trial-level artifact digests exist.
  TRAJ mission (build-program Phase 1) already specs loop-suspicion and
  zero-edit heuristics — this problem generalizes those.
- Plan sketch (v1 cheap, v2 real): v1 = post-hoc ATIF pass classifying
  each command mutating vs read-only (regex/heuristic), progress profile
  = cumulative mutating steps with changed observations. v2 = "shadow
  git" image instrumentation: init a git repo over /app at trial start,
  auto-commit after every agent command via shell hook baked into the
  task image — per-step diffs for ANY agent, no agent cooperation needed.
- Candidates: TOFFEE metadata structure (repo, verify via DD-TOFFEE);
  Docker checkpointing claims (UNVERIFIED).
- Verdict: build v1 in TRAJ now; RESEARCHER checks prior art before v2.

## P2 — Trajectory de-looping / dead-branch pruning ("graph refiner")
- Problem: agents loop and backtrack; raw trajectories are bloated; we
  want the "golden path" and explicit dead branches (dead branches are
  data too — they show where the agent got confused).
- What we have: loop-suspicion heuristic spec'd in TRAJ; nothing
  graph-based.
- Candidates: WebClipper graph-based trajectory pruning (UNVERIFIED,
  claimed Feb 2026); TOFFEE trajectory structuring (verify).
- Verdict: pending RESEARCHER brief. Do not build graph machinery before
  the simple version (repeat-state detection by observation hash) proves
  insufficient.

## P3 — Trajectory quality scoring (good vs bad beyond pass/fail)
- Problem: outcome-passing trajectories can still be garbage process
  (shortcuts, lucky passes, verifier fooling) — Peter's reward-hacking
  instinct, and the field agrees.
- What we have: Meta-Task F.3 judge prompt in inbox (KEEP/DISCARD:
  shortcutting, fabrication, unproductive); behavior module features;
  human labels as ground truth; judge calibration machinery.
- Candidates: Meta-Task F.3 (verified, in inbox); llm-as-a-verifier
  criteria templates (verified); TOFFEE cost-model claims (verify).
- Verdict: adapt F.3 + LaaV criteria into a calibrated judge lane once
  SEAM lands; heuristics first, judge second, human calibration always.

## P4 — Context pruning for long agent sessions
- Problem: huge tool outputs (500-line logs) poison context; agents
  reread their own mistakes.
- What we have: contextpack budgets (mission-time); nothing at
  tool-output level for solver agents (their harnesses own that);
  relevant mainly to OUR analysis/authoring missions.
- Candidates: SWE-Pruner 2601.16746 (UNVERIFIED); OMP built-in
  compaction/TTSR (verified, omp:// docs).
- Verdict: park for solver agents (their CLIs handle it); RESEARCHER
  brief only if our own missions show context-poisoning symptoms.

## P5 — Task archetypes / templates for generation from real data
- Problem: "write a Harbor task from X data" without structure →
  hallucinated dependencies, unverifiable tasks.
- What we have: authoring seed classes (mutation/scenario/craft-gap,
  inversion=class 4 planned); Meta-Task skeleton+exemplar (adopted);
  F.1 instruction template in inbox; craft facet space.
- Candidates: Meta-Task (verified); SETA-Synth source-conversion recipe
  (paper verified, recipe detail via DD-SETA); SWE-smith (verify code).
- Verdict: largely SOLVED in design; DD missions extract remaining
  specifics. No new build until EXPERIENCE A/B data arrives.

## P6 — Validating generated tasks before they count
- Problem: a generated task is garbage unless proven solvable, failable,
  and honest.
- What we have: THE BATTERY — oracle must pass, nop must fail,
  fair-oracle, adversarial pass, human promotion. The "validator loop"
  from Peter's pasted research (test must fail fresh, pass after golden
  agent) is a subset of what already runs.
- Verdict: SOLVED structurally; keep hardening via funnel evidence.

## P7 — Finding relevant prior work reliably
- What we have: radar doc (query battery, citation-walk, watch list) +
  this ledger + RESEARCHER mission.
- Verdict: solved procedurally as of tonight; quality proven by whether
  UNVERIFIED rows above get confirmed or killed within two weeks.
