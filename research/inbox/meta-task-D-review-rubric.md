---
source_url: https://arxiv.org/abs/2607.27929
source_type: paper
retrieved: 2026-08-19
license_note: "CC BY 4.0 (arXiv HTML states License: CC BY 4.0) — verbatim-quotable with attribution"
status: raw
feeds:
  - library/curated/standards/meta-task/D-review-rubric.md
---

# Meta-Task — Appendix D: task quality evaluation (19-criterion rubric)

Appendix ref: D of arXiv:2607.27929v1. Source: https://arxiv.org/html/2607.27929v1.

**Why this matters here.** This is the review pipeline our qualification ledger
field list should mirror. Two honest limits, both recorded rather than papered
over: (1) **the reviewer prompt itself is not published** — Appendix D describes
the protocol and names the 19 criteria, but no prompt text appears in the paper,
so any "their review prompt" we ever ship would be our reconstruction and must
say so; (2) the criteria are named but not individually defined, so mapping them
onto our battery requires our own definitions.

Their reported figures for context (Figure 4(a), 50 sampled tasks per method,
Claude Opus 4.6 as reviewer): Meta-Task highest implementation compliance at
66-72% with proposal acceptance 54-58%; CLI-Gym higher on proposal (64%) but 43%
implementation "due to lacking solution scripts".

## Verbatim (Appendix D)

```text
Appendix D Task Quality Evaluation Details

We employ Claude Opus 4.6 as an automated reviewer following the official Terminal-Bench review pipeline. For each synthesis method, we randomly sample 50 tasks and conduct two types of review:

Proposal Review. The reviewer reads the task description and evaluates whether the task idea is likely to be accepted into Terminal-Bench based on the official Task Proposal Rubric, which requires tasks to be verifiable, well-specified, solvable, appropriately challenging, realistic, and outcome-verified. The reviewer outputs Accept, Uncertain, or Reject.

Implementation Review. The reviewer reads the full task package (instruction, Dockerfile, solution, tests, task.toml) and evaluates it against the 19-criterion Task Implementation Rubric. The 19 criteria cover: verifiable, well_specified, solvable, difficult, novel, anti_cheat_robustness, interesting, agentic, functional_verification, outcome_verified, deterministic_reproducible, essential_difficulty, instruction_clarity, solution_quality, environment_hygiene, test_instruction_alignment, reviewable, structured_data_schema, and typos. Each criterion is scored as PASS, FAIL, or NA.

The implementation compliance rate reported in Figure 4(a) is the average PASS rate across all 19 criteria, reflecting the overall completeness and reliability of the synthesized task package.
```

## The 19 criteria, as a list (extracted from the paragraph above)

1. verifiable
2. well_specified
3. solvable
4. difficult
5. novel
6. anti_cheat_robustness
7. interesting
8. agentic
9. functional_verification
10. outcome_verified
11. deterministic_reproducible
12. essential_difficulty
13. instruction_clarity
14. solution_quality
15. environment_hygiene
16. test_instruction_alignment
17. reviewable
18. structured_data_schema
19. typos

Scoring is PASS / FAIL / NA per criterion; the compliance rate they report is the
average PASS rate across all 19. Proposal review is a separate, coarser gate
outputting Accept / Uncertain / Reject against the official Task Proposal Rubric
(verifiable, well-specified, solvable, appropriately challenging, realistic,
outcome-verified).
