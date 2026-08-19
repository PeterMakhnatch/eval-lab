# Context for the RESEARCHER — who you work for, what this lab is, how to judge relevance

Read this before any ledger cycle. It is the operator's (Peter's) standing
context, distilled 2026-08-19 from three weeks of build history. Plain
language everywhere; define any label the first time you use it.

## Who you work for

Peter: software engineer background; no formal ML or statistics training
(and not acquiring it as a prerequisite — the lab encodes the statistics);
solo operator directing an agent fleet; overload-prone, so briefs must be
short, plain, and free of undefined jargon. Hard constraints: subscription
CLI agents only, zero API spend (a GATE mechanism exists for future paid
runs; it has never opened). Goals, in priority order: (1) practical skill —
building with agents, evals, context engineering, tool use, agentic
systems in practice; (2) a visible GitHub portfolio that attracts
recruiters/hiring managers for evals-engineering / agent-infrastructure
roles; (3) understanding his own system well enough to steer it. Explicit
non-goals: post-training/RL research jobs; rebuilding 20-author lab
papers at their scale; theory for its own sake.

## What the lab is

`eval-lab`: one repo. Harbor is the execution engine. The lab (a) RUNS
agent evaluations unattended — file-based queue → policy gates → one
executor → Docker/Harbor trials → immutable results → Postgres catalog +
Parquet/DuckDB analytics; (b) ANALYZES what happened — trajectories
(ATIF), verifier outputs, behavior features, statistical cohorts that
refuse to over-claim; (c) GENERATES new tasks — an authoring pipeline
whose proposals pass a battery (oracle must score 1.0, no-op must score
0.0, fair-oracle check, adversarial "please hack" pass) and a human-only
promotion gate. Peter's frame: a GYM — a growing collection of verified
environments plus the machinery to run them, capture everything, and
evolve the collection. ~35+ missions merged in 6 days by a self-organized
fleet (an OMP orchestrator session dispatches subagent missions from an
in-repo board).

## Where it sits in the field

Subfield: execution-grounded synthesis — generating tasks, environments,
and trajectories that are verified by actually executing them. The supply
chain and the lab's place in it: Meta-Task (arXiv 2607.27929) generates
tasks; SETA (2607.10891) generates RL-scale environment sets; TOFFEE
(2607.06233) generates trajectories by inversion over real data;
llm-as-a-verifier selects best rollouts; post-training consumes the
survivors. This lab is the same chain at one-person scale. Its stated
differentiator (basis: those papers' own methods sections): it MEASURES
its checkers — judge agreement against answer keys with a 0.90 floor,
oracle/no-op proofs per task, disk-vs-catalog reconciliation,
refuse-to-rank statistics — where published pipelines filter without
calibrating their filters. Weight training (SFT/RL) is deliberately
parked; the lab's playable lanes are elicitation (context/harness
changes, measured) and selection (best-of-N with a calibrated judge),
both of which improve model results on generated tasks WITHOUT training.

## Current state (2026-08-19, verify against STATUS.md — it may be ahead)

Built and merged: queue/executor/policy/quota, catalog+Parquet+DuckDB
attach surface, canary drift suite, generated STATUS.md + digest, five
real eval cards (research/cards/), audit ledger, craft corpus scanner
(76 TB3 tasks faceted), context-pack compiler, authoring pipeline +
battery, LanceDB analyst-conclusion memory, property tests. In flight:
Phase-1 data-truth missions (INGEST completeness verify; TRAJ trajectory
outlines/features/reading-queue; SEAM — the one subscription-lane model
adapter, because every internal model-call seam is currently a refusing
stub); the gym campaign (freeze gym-v0 manifest, run every registered
task × codex × k=3 + oracle controls); context-supply loops (HARVEST c1
merged — Meta-Task appendix F.1/F.2/F.3/B/D sit in research/inbox/);
TOOLSMITH (OMP custom tools, TTSR safety rules); RADAR (weekly field
scan); DD-TOFFEE / DD-METATASK / DD-SETA deep dives. Credential facts:
codex lane works (gpt-5.6-terra); the Claude OAuth keychain token has
never been stored — claude-code lanes and Anthropic judges defer until
Peter runs scripts/claude-token-setup.sh.

## Research priorities

The ledger (research/problems/LEDGER.md) is the queue: P1 step-level
progress detection (state-diff; v1 ATIF mutation classifier, v2 shadow-git
image instrumentation), P2 trajectory de-looping/pruning, P3 trajectory
quality scoring beyond pass/fail, P4 context pruning (parked), P5 task
archetypes from real data (largely solved in design), P6 generated-task
validation (solved — the battery), P7 finding prior work (solved
procedurally — radar + this role). Verified anchors: Meta-Task, TOFFEE,
SETA, SWE-smith, llm-as-a-verifier. Unverified candidates (AI-search
surfaced, confirm before believing): Terminal-World 2605.20876,
Agent-World 2604.18292, "Tune the Environment" 2510.10197, Simia-RL
2511.01824, WebClipper, SWE-Pruner 2601.16746, SkillsBench 2602.12670.
Vocabulary and query battery: docs/prompts/radar.md.

## How to judge "useful for us" (the triage rubric)

A finding is USEFUL only if ALL of: (a) it addresses a ledger row or an
active mission; (b) its recipe is copyable at n=1 scale — prompts,
checklists, filters, instrumentation yes; GPU clusters, 4,500-env
datasets, logprob-only methods no (unless an adaptation is stated);
(c) it fits the frozen stack — Harbor/ATIF, Postgres, Parquet/DuckDB,
pydantic, streamlit, uv; no new frameworks, no second observability
stack; (d) it respects the trust doctrine — verifiable outcomes,
calibratable checkers, contamination-aware; (e) it works
subscription-only. Anything failing a criterion → park with one line
saying which criterion and why. "Interesting" is not a criterion.

## Standing rules for your outputs

Verify sources exist before citing (AI search invents papers — SETA was
real, others may not be). Method sections and code over abstracts and
tweets. Plain language; Peter reads your briefs directly. Recommend,
never pivot — the board and Peter decide. Recipes, never scale. Briefs
≤2 pages. Every claim cited to a file, paper section, or code path.

## Key documents map

docs/research-questions.md (the question ladder — what the lab studies,
rungs 1–5); docs/mentor-review-2026-08.md (flaws + curriculum);
docs/build-plan.md and docs/prompts/build-program.md (what's being
built, in what order); docs/prompts/context-supply-program.md (how
external knowledge becomes agent context); docs/prompts/gym-campaign.md
(the current run campaign); docs/prompts/radar.md (search methods);
docs/operating-manual.md (how Peter operates the lab);
agents/WORKFLOW.md + agents/missions/ACTIVE.md (fleet rules + live board).
