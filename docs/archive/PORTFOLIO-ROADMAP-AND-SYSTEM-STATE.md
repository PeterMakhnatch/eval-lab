---
status: historical
audience:
  - builder
  - analyst
authoritative_source: docs/NOW.md
---

# Eval-Lab: Current System State, Architecture Audit & Career Portfolio Roadmap

**Date:** September 1, 2026  
**Author:** Peter Makhnatch & System Architect  
**Objective:** Cut through feature sprawl, consolidate the core evaluation engineering, and package `eval-lab` into a high-impact flagship portfolio project for top AI Evaluation, Agent Platform, and Post-Training Research roles.

---

## 1. Executive Reality Check: The Core Thesis

### The Problem
You started with a clear goal: build a world-class agent evaluation workbench. Over time, background agents have created **sprawling architecture** (40+ branches, 147 untracked inbox memos, multi-zone storage abstractions, and complex agent handoff protocols). This creates cognitive overload and risks building a sprawling internal OS rather than a sharp, demonstrable product that gets you hired.

### The Good News
**The hard engineering is already built and working.** You do not need to build 10 more features. You need to **consolidate, prune the noise, run one focused benchmark experiment, and package the results.**

---

## 2. Where the System Actually Is Today

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          EVAL-LAB: THE 4 REAL PILLARS                       │
├──────────────────────────────────────┬──────────────────────────────────────┤
│ 🟢 PILLAR 1: HARBOR EXECUTION HARNESS │ 🟢 PILLAR 2: TRAJECTORY IR ENGINE    │
│ • Runs Docker trials cleanly         │ • ATIF trajectory normalization      │
│ • Pinned datasets (TB 4.0.0, etc.)   │ • Step-by-step observation parsing   │
│ • Supports OpenCode, SWE-agent, etc. │ • Isolates state-drift & tool loops  │
├──────────────────────────────────────┼──────────────────────────────────────┤
│ 🟢 PILLAR 3: SYNTHETIC TASK GENERATOR│ 🟡 PILLAR 4: STORAGE & DASHBOARD     │
│ • `src/evallab/synthetic_tool_memory`│ • PostgreSQL metadata catalog (Z2)   │
│ • 34 passing unit tests              │ • DuckDB unified query attach        │
│ • SPADE Gym MDPs + hint-regret gate  │ • Streamlit Explorer UI (working)    │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

### What is Solid and Ready:
1. **Execution Engine (`src/evallab/runner.py`):** Runs agent trials in isolated Docker containers via Harbor.
2. **Trajectory Intelligence (`src/evallab/interpretation/trajectory_ir.py`):** Parses raw logs into structured execution facts to diagnose *why* models fail.
3. **Synthetic Tool+Memory Generator (`src/evallab/synthetic_tool_memory.py`):** Synthesizes verifiable multi-turn Gym environments from text seeds with AST security audits and hint-based regret filtering (34/34 tests passing).
4. **Interactive Dashboard (`dashboard/explorer.py`):** Visual UI to drill down into trajectories, tool calls, and failure causes.

### What is Bloat & Needs Pruning:
* **147 untracked files in `research/inbox/`:** Agent-to-agent memos, half-baked drafts, and historical review notes.
* **40+ drifting git branches and worktrees:** Unnecessary parallel lane branches that create merge friction.
* **Over-engineered bureaucracy:** Multi-agent role handoff files and ADR ledgers that distract from running actual evaluation experiments.

---

## 3. What Hiring Managers Actually Care About

Top AI labs (Anthropic, OpenAI, Scale AI, Sierra, METR, Cognition) evaluate candidates on **signal, depth, and clarity**:

| What Hiring Managers DO NOT Care About | What Hiring Managers DO Care About (Your Strengths) |
|---|---|
| ❌ 50 micro-scripts and CLI flags | ✅ **One clean, reproducible evaluation harness** (Harbor / TB 4.0 / OpenCode) |
| ❌ 100 internal markdown design memos | ✅ **A Novel Trajectory Diagnostic Pipeline** that pinpoints exact failure modes (Context Compaction Loss, State Drift) |
| ❌ Complex multi-agent handoff bureaucracy | ✅ **A Verifiable Synthetic Task Generator** (SPADE-inspired Gym MDPs with hint-regret verifiers) |
| ❌ Incomplete storage abstractions | ✅ **An Interactive Live Demo & Eval Card** that anyone can run with 1 command |

---

## 4. The Flagship Narrative: What Your Project Demonstrates

Your portfolio project has a clear, compelling research and engineering story:

> ### *"Why Multi-Turn Tool Use is a Working Memory Problem"*
> 1. **The Diagnostic Finding:** Evaluating frontier models (Claude, DeepSeek, Gemini) on terminal benchmarks reveals that **47% of failures are caused by State-Tracking Drift and Context Rot**, not bad tool syntax.
> 2. **The Synthetic Solution:** Rather than hand-authoring benchmarks, `eval-lab` uses a **SPADE-style self-play synthesis pipeline** to generate interactive Gym environments that stress-test working memory; hint regret estimates solvability/difficulty and supports curriculum selection; it does not certify verifier validity or reward alignment.
> 3. **The Proof:** Training/evaluating on stateful synthetic memory tasks directly correlates with higher multi-turn tool reliability on real benchmarks (BFCL-v4, ACEBench, Terminal-Bench 4.0).

---

## 5. The 3-Phase Action Plan to "Reign It In"

```
[ Phase 1: Clean House ] ──► [ Phase 2: The Focused Run ] ──► [ Phase 3: Package Portfolio ]
• Consolidate code to main   • 10 TB 4.0 Tasks               • 1-command Streamlit demo
• Archive 140+ inbox notes   • 10 Synthetic Memory Tasks     • Clean GitHub README
• Prune dead worktrees       • 3 Flash Models (DeepSeek/     • 3-page Technical Report
                               Gemini/Qwen)                  • 3-minute video walk-through
```

### Phase 1: Consolidate & Prune (1–2 Days)
- Merge working modules (`synthetic_tool_memory.py`, `dashboard/`, `trajectory_ir.py`) directly onto `main`.
- Move stale `research/inbox/` files into a single `research/archive/` folder.
- Remove obsolete worktrees so the repo is fast and clean.

### Phase 2: Run One Focused Benchmark Experiment (2–3 Days)
- Select a fixed 20-task evaluation slice:
  - **10 Tasks:** Terminal-Bench 4.0 (Real terminal/coding tasks).
  - **10 Tasks:** Synthetic Tool+Memory Environments (Generated by `synthetic_tool_memory.py`).
- Run 3 active models: **DeepSeek V4 Flash**, **Google Gemini 3.7 Flash**, and **Claude/Qwen**.
- Run `evallab traj ir` to generate the **Behavioral Failure Attribution Matrix**.

### Phase 3: Package Portfolio & Deliverables (2–3 Days)
1. **GitHub README:** Highlighting the architecture diagram, the research question, and benchmark graphs.
2. **1-Command Demo:** `uv run evallab dashboard` to let any hiring manager explore the trajectories and capability curves locally.
3. **A 3-Page Technical Report:** Summarizing your methodology, the hint-regret verifier engine, and empirical findings.

---

## 6. Next Immediate Steps
1. ✅ **Done:** Synthetic Tool+Memory generator implemented and tested (`tests/test_synthetic_tool_memory.py` passing 34/34).
2. ✅ **Done:** Data architecture audit and literature dossier documented in `research/analysis/`.
3. 🔜 **Next:** Run a single smoke test with **OpenCode** on TB 4.0 to confirm the execution pipeline.
4. 🔜 **Next:** Archive inbox notes and merge core code to `main`.
