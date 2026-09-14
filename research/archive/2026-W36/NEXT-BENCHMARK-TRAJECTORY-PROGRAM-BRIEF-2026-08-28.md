---
type: execution-brief
topic: focused-benchmark-trajectory-program
date: 2026-08-28
status: distilled
owner: OMP Main
program_status: active
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-28
license_note: Internal execution brief; Eval Lab repository license applies.
feeds:
  - parked
---

# Focused Benchmark and Trajectory Program

## Goal

Move Eval Lab from instrument-building to sustained data collection and benchmark-specific learning using DeepSeek V4 Flash. Focus on three constructs:

1. context and actionable memory;
2. tool selection, composition, and value propagation;
3. error detection, diagnosis, and autonomous recovery.

The deliverable is not a broad leaderboard. It is three complete vertical capabilities: runnable benchmark cohorts, lossless evidence, benchmark-specific fact and derivative-feature producers, analysis surfaces, repeated campaigns, and validity gates.

## Current evidence boundary

- P1 lossless IR/loss manifest, P2 error semantics, P3 state/reference validity, the feature registry, and deterministic backfill are merged.
- FuncDAG easy has a successful DeepSeek Flash canary.
- Tau has full oracle/nop/adversarial certification but not an official live user-simulator model run.
- Existing data is too short and too old to populate state, recovery, and open-model fields adequately.
- C0 structural facts support description and screening. C1 requires task contracts. Causal language requires matched single-delta or dose-ladder interventions.
- Every rate requires an explicit denominator sibling and null on zero opportunity.
- No cross-benchmark pooled score and no LLM-judged labels.

## Required decisions

For each construct, recommend one primary benchmark and one fallback. A primary must have:

- public and license-compatible source/runtime;
- tasks that can be Harbor-hosted and run with DeepSeek Flash;
- observable opportunities and deterministic outcomes;
- enough task or perturbation variation for repeated cells;
- a credible path to state-journal and ATIF coverage;
- a benchmark-specific feature family that changes a research decision.

Prefer an existing validated benchmark when it isolates the construct. Prefer a certified synthetic family when external assets are unavailable, lossy, or confounded.

## Candidate direction to challenge

- Context/memory: LOCA-Bench or another source-verified actionable-memory benchmark, with neutral-padding, semantic-distractor, position, and forced-compaction arms kept separate.
- Tool selection/composition: FuncDAG/FuncBenchGen, expanded into depth, width, distractor-surface, schema-drift, and value-propagation cells.
- Error/recovery: an AgentCheck/ToolBench-X-style single-fault suite if source assets are usable; otherwise a Harbor-native certified MCP fault-injection family with permission, not-found, timeout, malformed-output, and silent-wrong-result cells.

## Analyst assignment

Produce `research/inbox/NEXT-BENCHMARK-PROGRAM-ANALYST-REPLY-2026-08-28.md` containing:

1. ranked benchmark portfolio and rejection reasons;
2. construct -> opportunity -> L1 facts -> L2 features -> L3 interventions;
3. denominator and evidence grade for every proposed rate;
4. required task counts, repeats, strata, and a power-planning method rather than guessed sample sizes;
5. exact first campaigns and the decisions each result changes;
6. features deliberately deferred until data supports their order.

Coordinate with Tutor before finalizing. Send Tutor the draft and incorporate the strongest measurement objections.

## Tutor assignment

Produce `research/inbox/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md` containing:

1. strongest confounds and category errors in the candidate direction;
2. whether each benchmark isolates the named construct or mostly measures task difficulty/scaffold behavior;
3. falsification arms, negative controls, and discrimination gates;
4. minimum conditions before any capability, causal, or comparative claim;
5. concrete revisions to Analyst's draft.

Review Analyst's draft when available and request one revision round. Focus on measurement validity, not wording.

## Handoff targets after benchmark selection

- Architect: system topology, immutable schemas, campaign/control flow, failure isolation, and promotion gates.
- Eval Platform / Agent Data: producers, feature mart, run manifests, resume/idempotency, analysis APIs, and migration.
- Eval Runner: bounded but long-running DeepSeek campaigns with fair timeouts, cost caps, secret sanitizer, durable manifests, and deterministic backfill.
- Gemini and Grok builders/reviewers: independent implementation and adversarial validation in isolated worktrees.

## Completion contract

A vertical is complete only when it can:

1. materialize or select an immutable cohort;
2. run oracle/nop/negative controls;
3. run DeepSeek Flash repeatedly with resume and explicit budgets;
4. archive sanitized native + ATIF evidence;
5. populate its core and derivative feature tables with coverage diagnostics;
6. execute benchmark-specific analyses with uncertainty and refusal-to-rank behavior;
7. regenerate deterministically; and
8. emit a review packet without registering or publishing automatically.
