---
status: living
audience:
  - builder
  - operator
---

# Implementation Prompts & Living Work Orders

This directory holds the living implementation prompts, campaign directives, and recurring loop protocols for Eval Lab agents.

The authoritative live mission board is [`agents/missions/ACTIVE.md`](../../agents/missions/ACTIVE.md). Completed, superseded, and retired historical briefs have moved to [`docs/archive/prompts/`](../archive/prompts/).

## Living Prompts & Directives

| Prompt / Directive | Audience / Role | Scope & Operational Invariants |
|---|---|---|
| [`build-program.md`](build-program.md) | Builder | Forward autonomous implementation loop directives and safety invariants. |
| [`context-loops.md`](context-loops.md) | Analyst / Researcher | Context-supply integration loops and research distillation workflows. |
| [`context-supply-program.md`](context-supply-program.md) | Researcher / Builder | Multi-cycle context-supply program specification (HARVEST, STANDARDS, VERIFIER, PACK). |
| [`gym-campaign.md`](gym-campaign.md) | Builder / Runner | Task capability acquisition, Harbor environment parity, and gym benchmarks. |
| [`night-loops.md`](night-loops.md) | Operator / Autopilot | Nightly execution loop cadence, safety checks, and RECHECK-to-RECORD cadence. |
| [`omp-toolkit.md`](omp-toolkit.md) | Builder / Toolsmith | Oh-My-Pi harness integration, MCP conventions, and tool policies. |
| [`radar.md`](radar.md) | Researcher | Standing literature radar and upstream paper/repo survey protocol. |
| [`researcher.md`](researcher.md) | Researcher | Standing prior-art and empirical investigation agent guidelines. |
| [`synthesis-build.md`](synthesis-build.md) | Builder / Architect | Synthetic dataset generator (SG-1..4) build directives. |

## Historical Briefs & Mission Archive

- **Archived Briefs**: Completed implementation briefs (01–09, 12) and legacy dated dispatch files (overnight, wave3, functionalization) are archived at [`docs/archive/prompts/`](../archive/prompts/).
- **Unwritten Briefs**: Briefs 10 and 11 were never written in the historical sequence.
- **Untracked / Scrap Files**: `docs/prompts/Untitled` is a proven-unused 30-byte legacy scrap file preserved in place for a subsequent repository-wide dead-code deletion pass.
- **Board Authority**: `agents/missions/ACTIVE.md` is the single authoritative source of truth for active missions, assigned role lanes, hard stops, and current system gates.
