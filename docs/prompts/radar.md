---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Radar — finding the field's output before it finds us

Continuous literature/field scanning as a LAB FUNCTION (a weekly loop with
a seen-registry), not as Peter-anxiety. Written 2026-08-19 after Google
surfaced five sibling papers to Meta-Task/TOFFEE and SETA was verified
against arXiv directly.

## Anchor set (the papers this lab is "near")

Verified: **Meta-Task** (2607.27929 — task synthesis as a terminal task),
**TOFFEE** (2607.06233 — trajectory synthesis via inversion over real
data), **SETA** (2607.10891 — verified 2026-08-19: SETA-Synth converts
sources into standardized verifiable environments, SETA-Evol expands with
adaptive difficulty; released SETA-Env, 4,500+ environments; Qwen3-8B +
GRPO → 12% TB 2.0), **llm-as-a-verifier** (repo; selection/reward layer),
**SWE-smith** (task generation from real repos).

Candidates, verify at intake (from Google's answer — each needs its
abstract confirmed before an inbox note): Terminal-World (2605.20876,
skill-grounded env synthesis), Agent-World (2604.18292, co-evolution /
gap-finding arena), "Don't Just Fine-tune the Agent, Tune the Environment"
(2510.10197, environment-side curriculum), Simia-RL (2511.01824,
LLM-simulated environment feedback).

## Keyword clusters (the field's vocabulary — use in every sweep)

- **Core:** execution-grounded · verifiable environments · environment
  synthesis / environment generation / environment scaling · task
  synthesis · terminal agent(s) · agent trajectories · trajectory
  synthesis / trajectory filtering · verifiable rewards / RLVR · agentic
  RL · RL environments
- **Selection/reward layer:** LLM-as-judge / LLM-as-verifier · reward
  model for agents · best-of-N selection · self-verification · process
  reward / progress reward
- **Evolution layer:** co-evolution · adaptive difficulty · curriculum
  generation · self-improving agents · capability gap discovery
- **Supply chain:** synthetic data for agents · SFT trajectories · agent
  distillation · benchmark contamination · held-out evaluation
- **Named systems to citation-walk:** Meta-Task, TOFFEE, SETA, SWE-smith,
  SWE-Gym, Terminal-Bench, AgentGym, Agent-World, Terminal-World,
  Simia-RL, CLI-Gym, TerminalTraj, Nemotron-Terminal, SkillSynth (the
  Meta-Task comparison-table set).

## Query battery (run verbatim, then vary one term)

1. arXiv listing sweep: `site:arxiv.org ("environment synthesis" OR
   "task synthesis") (terminal OR agent) 2026`
2. `site:arxiv.org "verifiable" "reinforcement learning" environments
   terminal-bench`
3. `"terminal-bench" (synthesis OR generation OR RL) -site:github.com` —
   TB is the field's shared benchmark; new work reports on it.
4. GitHub: topic/code search `terminal-bench synthesis`, `agent
   environment generation`, `verifiable environments dataset`; check
   awesome-lists updated in the last 90 days.
5. HuggingFace daily papers search: "environment", "agent RL",
   "trajectories" — weekly skim of titles only.
6. X search: `"environment synthesis" filter:links`, `"terminal-bench"
   filter:links min_faves:20` — the field announces on X before it
   indexes anywhere.

## Citation-walking (highest yield per minute; do this FIRST)

On Semantic Scholar or alphaXiv, open each anchor paper and read (a) its
"cited by" list newest-first, (b) its reference list's 2026 entries. Every
paper in this subfield cites Meta-Task or TOFFEE or SETA within months.
One quarterly walk of three anchors beats fifty keyword searches. Also:
each anchor's comparison table (like Meta-Task's Table 1) is a curated
sibling list — mine those names.

## Watch list (follow, don't doomscroll)

Paper firehoses: @_akhaliq, @arankomatsuzaki (X) — titles only, weekly.
Field voices: @jackyk02 (llm-as-a-verifier author), @neversupervised
(Bercovich — Peter's flagged sane voice), the Terminal-Bench/Harbor
community accounts + Discord announcements channel. HF daily papers page.
alphaXiv trending, weekly skim.

## RADAR — the loop mission

**Lease:** `research/radar/**` (seen-registry.json, weekly log), inbox
appends via HARVEST's queue file (one-line candidates), no other writes.

**Weekly cycle (≤90 min):** (1) citation-walk any anchor not walked in 90
days; (2) run the query battery, diff hits against seen-registry; (3)
triage each new hit in ≤3 sentences: relevant-now (→ HARVEST queue with
`feeds` guess) / relevant-later (→ registry with tag) / noise (→ registry,
reason one line); (4) verify any Google/AI-surfaced candidate against its
actual abstract before it enters the queue (AI search answers invent
papers; SETA was real, the next one may not be); (5) append the weekly log:
hits, verdicts, one "field motion" sentence. **Never:** pivot lab scope
from a radar finding — findings feed HARVEST/board backlog only; scope
changes remain Peter-level decisions.

**Acceptance (standing):** registry grows monotonically; zero duplicate
triage; the four candidate papers above verified and disposed within two
cycles; weekly log line lands in the digest via board-note to SURFACE.

## DEEP-DIVE missions (one-shot, spawn when slots allow)

**DD-TOFFEE** — clone the repo read-only; anatomy doc: pipeline stages,
the inversion mechanism (answer-first over real data), model-selection
loop, what verifies each stage; then a REPLICATION SPEC: seed_class 4
(inversion) implemented against one of our real data assets, battery-
gated, pre-registered with expected pass-rate range. Output: anatomy doc
in research/papers/, replication spec on the board.

**DD-METATASK** — no public code; deep-extract from paper+appendices (in
inbox): filtering thresholds, exemplar-pool construction and manual
verification protocol, completeness-checker specifics, phase-2 spec-design
sampling details. Output: delta table "what SG-1/authoring does differently
and whether each delta is deliberate" — improvement proposals as board
backlog items, not direct edits.

**DD-SETA** — paper deep-read: SETA-Synth source-conversion recipe vs our
fetch/authoring; SETA-Evol difficulty-evolution vs our EXPERIENCE/craft-gap
seeding; their verification mechanism vs our battery. Output: adopt/skip
table (same format as the SWE-smith gate comparison) + candidate: is
SETA-Env pullable as an external corpus (fetch ≠ register)?

**LaaV** — already owned by LOOP-VERIFIER; add one cycle there if absent:
read the repo code (not just README) for the criteria templates and PPT
implementation details.

## Dispatch prompt (append to the orchestrator's queue)

> Read docs/prompts/radar.md. Register RADAR as a standing weekly mission
> (lease as written) and run cycle 1 now: citation-walk Meta-Task +
> TOFFEE + SETA, verify the four candidate papers, triage into the
> HARVEST queue. Register DD-TOFFEE, DD-METATASK, DD-SETA as ready
> one-shot missions to run as slots allow, priority after Phase-1 builds.

## Changelog

- 2026-08-19 — v1: anchors verified (SETA confirmed against arXiv),
  vocabulary + query battery + citation-walk protocol + RADAR loop +
  three deep-dives.
