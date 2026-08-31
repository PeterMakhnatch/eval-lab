---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-31
license_note: Internal Eval Lab research brief; repository license governs.
status: distilled
feeds:
  - parked
---

# Eval Lab thematic benchmark brief

## Goal

Recommend a coherent benchmark portfolio for Eval Lab. The user does not want ten unrelated leaderboards; they want a small thematic program with roughly three main topics, plus only genuinely adjacent benchmarks.

## Current candidates

- RSI-Exam
- RE-Bench
- PaperBench
- MLE-bench
- CORE-Bench
- AgentBoard
- ToolSandbox
- Inspect Evals tasks such as GAIA, OSWorld, Tau2, SWE-bench and MLE-bench

Read `research/analysis/agentic-benchmark-feature-inventory-2026-08-31.json` and the two RSI pilot evidence files. Verify benchmark descriptions against primary sources where needed.

## Questions

1. Which three themes best match Eval Lab's trajectory/data/research strengths and Peter's goal of studying agents rather than collecting unrelated scores?
2. For each theme, identify one anchor benchmark, up to three supporting benchmarks, the shared constructs, and the minimum comparable features.
3. Which candidate benchmarks should be deferred because they measure a different capability, are redundant, have weak trajectory access, or create excessive environment cost?
4. Identify newer or omitted benchmarks only when they materially strengthen one of the chosen themes.
5. Distinguish benchmark execution lanes: native Harbor, native Inspect, Inspect-Harbor parity, and import-only evidence.
6. Recommend a small initial corpus and an expansion rule based on information gain rather than benchmark popularity.

## Deliverable

Write a concise source-linked recommendation at `research/inbox/benchmark-themes-librarian-reply.md`, including a keep/defer table and claim boundaries. Do not edit production code or run tests. Page `OMP - main` when ready.
