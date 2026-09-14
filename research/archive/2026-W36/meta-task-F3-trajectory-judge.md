---
source_url: https://arxiv.org/abs/2607.27929
source_type: paper
retrieved: 2026-08-19
license_note: "CC BY 4.0 (arXiv HTML states License: CC BY 4.0) — verbatim-quotable with attribution"
status: raw
feeds:
  - library/curated/standards/meta-task/F3-trajectory-judge.md
  - library/curated/standards/verification/criteria-templates.md
---

# Meta-Task — Appendix F.3 / Figure 8: trajectory-level LLM-as-Judge prompt

Appendix ref: F.3, Figure 8 of arXiv:2607.27929v1. Source: https://arxiv.org/html/2607.27929v1
(inline SVG, extracted).

**Why this matters here.** The KEEP/DISCARD judge over *successful* trajectories.
Their ablation (Table 3) is the load-bearing number: filtering 5,004 passed
trajectories down to 3,221 gained +3.3 Avg Pass@1 and +3.4 Pass@3 on
Terminal-Bench 2.0 — quality over quantity, measured. Feeds both the EX-MT
corpus target and the verification corpus, since it is a judge-prompt shape.

## Verbatim (Figure 8)

```text
Trajectory Quality Review Prompt
System: You are a training data quality reviewer for AI agent trajectories. We are training an AI agent (via SFT) to solve terminal-based tasks in Docker containers. Each trajectory is a sequence of (observation, action) pairs. We want to keep only trajectories that teach GOOD problem-solving behavior. The core question: Does this trajectory demonstrate the kind of behavior we want the trained model to replicate?
What Makes a GOOD Trajectory (keep):
- Understanding a problem through exploration and reasoning
- Implementing solutions by writing its own code
- Testing, diagnosing failures, and iterating with meaningful changes
- Making progress through its own capability, not through information shortcuts
What Makes a BAD Trajectory (discard):
Shortcutting (agent avoids doing the actual work):
- Agent obtains answers from the environment rather than computing them — reading files that contain pre-computed results, answers, hints, or solution algorithms
- Agent reads files with red-flag names (“solution”, “fixed”, “answer”, “expected_output”, “correct”, “bugfix”, “hint”, “cheat”)
- Agent runs pre-existing scripts that produce the required output rather than writing its own implementation
- Agent sees computed values in data files and directly uses them as output without independently deriving them
- Agent reads source code comments that explicitly state the bug and correct fix (e.g., “BUG: should be X instead of Y”)
Fabrication (agent produces fake results):
- Agent claims to have verified or computed something but terminal shows it never ran
- Agent produces specific numerical values with no derivation trace anywhere in the trajectory
- Agent marks task complete despite terminal being stuck or non-functional
- Agent writes “verification passed” when the actual test command failed or was never executed
Unproductive behavior (low learning signal):
- Agent repeats the same approach many times without meaningful adaptation
- Agent sends >>10 consecutive turns of empty commands, Ctrl+C/Ctrl+D without recovery
- Agent tries the same general approach >>15 times without strategy changes
- Trajectory is too short to contain substantive work
Key Distinction: Reading input data that the agent needs to PROCESS (log files, databases, CSV data) is NOT contamination. Reading reference documentation (API docs, library source code) is NOT contamination. The key test: does the file contain the ANSWER to what the agent is supposed to produce, or does it contain RAW DATA that the agent must still reason about?
Output: KEEP or DISCARD with brief justification.
```
