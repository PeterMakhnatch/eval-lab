# The no-training map — eight lanes between "tasks exist" and "weights change"

Plain-language map of the mini-subfields Peter can work in WITHOUT
touching model training, with every paper from the R&D pile assigned to
its lane. Written 2026-08-19. UNVERIFIED = surfaced by AI search, not yet
confirmed by RESEARCHER.

The one-sentence orientation: the field's endgame is RL on model weights,
but every RL result stands on a stack of no-training layers — and that
stack is where this lab lives. The RL wave INCREASES demand for these
layers; it does not obsolete them.

## Lane 1 — Task & environment generation (benchmark engineering)
Making verifiable tasks/environments, by hand and by agent: archetypes,
skeletons, difficulty, contamination control.
- In the lab: authoring pipeline, seed classes, battery, craft corpus,
  gym-v0.
- Papers: Meta-Task 2607.27929 (task synthesis as a terminal task);
  SETA 2607.10891 (env generation at RL scale — read for the Synth
  recipe, ignore the scale); SWE-smith (tasks from real repos);
  Terminal-Bench discussion #224 + "good benchmarks" 2607.12217 (the
  craft canon); TOFFEE 2607.06233 (inversion: answer-first from real
  data); Terminal-World 2605.20876 UNVERIFIED (skill-grounded synthesis).
- Contribute here: yes — this is the lab's center. The hand-authored
  held-out set and the measured generation funnel are portfolio-grade.
- Find more: citation-walk Meta-Task/SETA; query "task synthesis"
  "verifiable environments" terminal.

## Lane 2 — Verifier engineering
Deterministic checkers, hybrid graders, judge calibration, reward-hack
resistance, verifier-validity testing.
- In the lab: battery (oracle/nop/fair-oracle/adversarial), judge
  calibration vs answer keys (0.90 floor), Reward Kit criteria.
- Papers: llm-as-a-verifier repo (criteria templates, score-token
  method); judge-reporting 2511.21140 (how to report judge-scored
  results honestly); Meta-Task appendix D (19-criterion review);
  TB rubric.
- Contribute here: yes, strongly — verifier validity is underbuilt
  field-wide (METR showed SWE-bench verifiers accept wrong patches).
  A reusable "verifier hardening kit" is a real artifact.
- Find more: "reward hacking" agents; "verifier" "validity" benchmark.

## Lane 3 — Trajectory analysis (agent behavior measurement)
What actually happened in a run: failure taxonomies, progress
measurement, loop detection, state capture, behavioral features.
- In the lab: TRAJ mission (outlines, features, reading queue), behavior
  module, failure taxonomy, ledger P1 (state-diff / shadow-git) and P2
  (de-looping), Meta-Task F.3 judge (in inbox).
- Papers: TOFFEE (trajectory structuring — verify details via DD-TOFFEE);
  WebClipper UNVERIFIED (graph-based pruning); Hamel/Shreya error-analysis
  method (the read-label-cluster-convert procedure).
- Contribute here: YES — flagship candidate. Per-step environment-truth
  capture for terminal agents (shadow-git) is agent-agnostic, novel-ish
  in practice, pure SWE, and everyone doing Lanes 1/2/5 wants it.
- Find more: "trajectory" (pruning OR filtering OR analysis) agents;
  "agent behavior" evaluation.

## Lane 4 — Harness & elicitation engineering
Same model, different scaffold: skills, tools, prompts, preambles,
attempt budgets — measured as experimental variables.
- In the lab: elicitation ladder, EXP-S03 preamble A/B, experience packs,
  OMP toolkit, repo skills.
- Papers: METR elicitation gap (the naive-vs-elicited delta dwarfs
  model-version deltas); scaffold-effect 2607.22585 (±10–20% pass rate
  from harness details); SkillsBench 2602.12670 UNVERIFIED (claimed:
  curated skills 33.9%→50.5%); Anthropic demystifying-evals; Lilian Weng
  harness post.
- Contribute here: yes — cheapest real experiments available to the lab;
  every result doubles as practical context-engineering skill.
- Find more: "elicitation" OR "scaffold" OR "harness" agent evaluation;
  "agent skills".

## Lane 5 — Inference-time selection & routing
Improving results with zero training: best-of-N, reranking,
self-verification, early stopping, model routing.
- In the lab: LOOP-VERIFIER adaptations (k-sample judge approximation,
  self-check instruction blocks, progress heuristics), pre-registered
  rerank experiment.
- Papers: llm-as-a-verifier (TB2.1 78.7%→88.0% at best-of-5, no
  training — the lane's proof of value); don't-pass@k 2510.04265
  (how to report attempt-based results).
- Contribute here: yes, once SEAM lands — rerank-on-own-trials is a
  clean, publishable-as-blog experiment.
- Find more: "best-of-n" OR "self-verification" agents; "test-time"
  selection agent.

## Lane 6 — Context & experience curation
What agents are fed: packs, memory, experience compilation, context
budgets, pruning.
- In the lab: context-supply program (HARVEST→STANDARDS→PACK), experience
  packs, LanceDB analyst memory, standards corpus.
- Papers: SWE-Pruner 2601.16746 UNVERIFIED (context pruning); Simia-RL
  2511.01824 UNVERIFIED (LLM-simulated feedback — adjacent, probably
  park); Manus/Anthropic context-engineering posts (in salvage).
- Contribute here: yes — the measured-packs A/B (does compiled context
  raise generation quality?) is exactly the kind of small real result
  the field hand-waves.
- Find more: "context engineering" agents; "agent memory" curation.

## Lane 7 — Eval methodology & statistics
Making numbers honest: intervals, clustering, power, pairing,
contamination reporting, eval cards.
- In the lab: cohort (cluster bootstrap, paired, power, refuse-to-rank),
  eval cards, contamination fields, human baselines (F5).
- Papers: statsforevals 2411.00640 (clustered SEs, paired differences);
  don't-use-CLT 2503.01747 (small-n intervals); judge-reporting
  2511.21140; METR SWE-bench-PRs note (verifier validity is fragile).
- Contribute here: yes as PRACTICE (every card), not as new methods —
  the methods exist; the lab's edge is actually applying them.
- Find more: "error bars" LLM evaluation; "contamination" benchmark.

## Lane 8 — Infrastructure & observability
The plumbing: sandboxing, capture, catalogs, funnels, dashboards,
unattended operation.
- In the lab: the whole platform (queue/executor/policy/quota, catalog,
  Parquet, STATUS/digest, explorer UI, funnel telemetry).
- Papers: none needed — this lane is engineering, and the lab is already
  its own best reference. Harbor/Phoenix docs are the canon.
- Contribute here: it's the portfolio's skeleton; write-ups of the
  mechanisms (policy-in-tool-schema, shadow-git, the battery) are the
  contribution.

## How the lanes stack (why no-training work stays valuable)

RL training (parked, not our layer)
  ▲ consumes: environments (L1) + verifiers (L2) + curated trajectories (L3/L6)
Selection (L5) improves results NOW using verifiers (L2)
Elicitation (L4) improves results NOW using tasks (L1) + measurement (L7)
Everything runs on infrastructure (L8) and reports through methodology (L7)

Every published RL result consumes lanes 1–3 and 6–8. SETA trained a
model; SETA-Synth (lane 1) and its verification (lane 2) are what made
that possible. The no-training layers are not the consolation prize —
they are the supply chain the training layer cannot exist without.
