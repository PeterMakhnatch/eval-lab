---
source_url: https://arxiv.org/abs/2607.27929
source_type: paper
retrieved: 2026-08-19
license_note: "CC BY 4.0 (arXiv HTML states License: CC BY 4.0) — verbatim-quotable with attribution"
status: raw
feeds:
  - library/curated/standards/meta-task/F2-spec-design-prompt.md
---

# Meta-Task — Appendix F.2 / Figure 7: multi-phase spec design prompt

Appendix ref: F.2, Figure 7 of arXiv:2607.27929v1. Source: https://arxiv.org/html/2607.27929v1
(inline SVG, extracted).

**Why this matters here.** This is the prompt that *designs new specs* rather
than tasks — Phase 1 of their multi-phase mode, seeded by a topic sample
(~2,000 fine-grained technical topics) and a scenario constraint (~120 style
descriptions), which they report extends the space past 240,000 seed
combinations. It is the direct analogue of our `design_novel_spec()` seam in
`authoring.py`, whose default designer is a deterministic stub today.

## Verbatim (Figure 7)

```text
Phase 1: Generate a New Category and Scenario
You are a creative task designer. Your job is to generate a category template and a scenario template that will guide the creation of a terminal-based evaluation task where an AI agent works in a Linux Docker container, using the terminal to accomplish goals through code, commands, and tools.
These tasks test an agent’s ability to plan, explore, and accomplish goals. Given a goal (sometimes vague), the agent must figure out how to achieve it by exploring the environment and making decisions on its own.
Target difficulty: $difficulty.
$difficulty_requirements
Your task patterns and examples should reflect this difficulty level. Your output is a starting point that another agent will build on, so aim for diversity and creativity.
Your Topic: $topic
Generate a category template around this topic. Below is a reference category template (on a different topic) showing the format and level of detail to aim for:
$category_ref
Reference Scenario Template
Below is an example scenario template showing the format. Generate a new scenario template with a different style.
$scenario_ref
Your Task
Write your output to the file /app/phase1_output.md containing a new category (around the topic $topic) and a new scenario.
For the category: Build around $topic. Come up with diverse task patterns, tools, data types, and challenges specific to this topic. Use the reference above only for format guidance.
For the scenario: Your scenario must follow this constraint:
$scenario_constraint
Design a scenario template that produces instruction.md files matching the constraint above. The scenario should specify the writing style, detail level, tone, and information strategy.
Output format:
=== CATEGORY ===
### Category: [Name]
[Your category content]

=== SCENARIO ===
For the ‘instruction.md‘ you generate, use a
**[style name]** style:
[Your scenario content]
```
