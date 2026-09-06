---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-31
license_note: Internal Eval Lab research brief; repository license governs.
status: distilled
feeds:
  - parked
---

# Eval Lab feature-analysis brief

## Goal

Write a technical Markdown guide that explains Eval Lab's current feature and derived-feature strategy, what the 74-feature `autonomous-research-v1` family actually measures, which features matter most, and where analysis capability should go next.

## Required evidence

Read these repository artifacts before forming conclusions:

- `research/analysis/agentic-benchmark-feature-inventory-2026-08-31.json`
- `research/evidence/rsi-bbo-codex56-calibration-2026-08-31.json`
- `research/evidence/rsi-game2048-codex56-calibration-2026-08-31.json`
- `src/evallab/autonomous_research.py`
- `src/evallab/interpretation/feature_registry.py`
- `src/evallab/multi_eval.py`

## Questions to answer

1. What is the difference between a raw fact, a derived feature, a semantic hypothesis, a benchmark outcome, and a decision feature?
2. Which of the 74 features are core cross-benchmark primitives versus benchmark-specific or currently low-value?
3. What did the BBO and Game2048 pilots reveal about missing analysis capabilities and misleading scalar status/reward summaries?
4. Should Eval Lab prioritize analysis infrastructure now, then add or prune features based on observed experiments? Give a concrete staged recommendation.
5. Propose a feature-governance loop: experiment → fact completeness audit → derived analysis → decision/usefulness review → retain/refine/prune.
6. Propose a thematic benchmark portfolio centered on a small number of research questions rather than a benchmark zoo.
7. Define the dashboards/tables/analyses an operator should be able to run next.

## Required position

Treat the current 74 features as an inventory, not 74 equally important KPIs. Preserve explicit denominators, source revisions, artifact bindings, score directions, and null-on-unknown semantics. Do not recommend a universal capability score.

## Deliverable

Write a substantive draft at `research/inbox/feature-analysis-meta-analyst-reply.md`. Use compact tables and an explicit recommended roadmap. Do not edit production code or run test suites. Page `OMP - main` when the file is ready.
