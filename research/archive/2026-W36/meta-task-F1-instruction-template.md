---
source_url: https://arxiv.org/abs/2607.27929
source_type: paper
retrieved: 2026-08-19
license_note: "CC BY 4.0 (arXiv HTML states License: CC BY 4.0) — verbatim-quotable with attribution"
status: raw
feeds:
  - library/curated/standards/meta-task/F1-instruction-template.md
  - _proposed_templates/meta-instruction-v1.md
---

# Meta-Task — Appendix F.1 / Figure 6: synthesis instruction template (fixed parts)

Appendix ref: F.1, Figure 6 of arXiv:2607.27929v1 ("Meta-Task: Turning Terminal
Task Synthesis into a Terminal Task for Scalable Agent Training", Pan et al.,
2026-07-30). Source: https://arxiv.org/html/2607.27929v1 — the figure is inline SVG in the HTML build;
text below is extracted from that SVG, not retyped.

**Why this matters here.** This is the centerpiece of EX-MT per Peter's explicit
direction: the fixed half of the prompt that makes an agent produce a whole
Terminal-Bench task package. Their "Self-Validation (REQUIRED)" checklist is the
part our battery vocabulary maps onto (oracle must pass, nop reasoning,
answer-leakage scan, cross-component consistency), and our two additions they
lack are fair-oracle framing and the adversarial "please hack" pass. Landed
verbatim; adaptation is STANDARDS' job, not intake's.

## Verbatim (Figure 6)

```text
Meta-Task Instruction (Fixed Parts)
# Task: Generate a Terminal-Based AI Agent Task
You are an expert in [CATEGORY] with deep knowledge of best practices, common patterns, and real-world challenges in this domain. Your task is to create a high-quality terminal-based task that tests an AI agent’s ability to solve practical problems in a containerized environment.
## 1. Reference
Study the following examples in /app/examples/: [EXAMPLE_LIST]. Start by exploring these. The real example shows the expected format, structure, and quality level. Do not imitate the example’s content, topic, or problem design. Create something completely original.
## 2. System Architecture
Your generated task is a self-contained package used in three stages:
- Synthesis Environment (current): You create task files in /app/output/. You can run solve.sh and tests to validate your task. You do NOT have Docker permissions here.
- Agent Execution Environment: Built using docker build -f environment/Dockerfile. The solving agent works here to complete instruction.md. Files from environment/ are COPYed into the container. Agent has full terminal access but limited time.
- Test Verification:tests/ directory is mounted at /tests/. test.sh runs pytest /tests/test_outputs.py and writes 1 (pass) or 0 (fail) to /logs/verifier/reward.txt.
The task must be fully self-contained. A fresh environment is built from your Dockerfile alone. All input data, dependencies, config files, and code referenced in instruction.md must be present inside the built container — either COPYed from environment/ or generated during docker build.
## 3. Output Structure — Generate a [DIFFICULTY] task in /app/output/:
/app/output/
|-- instruction.md # Task description (absolute paths)
|-- task.toml # Metadata (difficulty, category, tags)
|-- environment/
| |-- Dockerfile # FROM, WORKDIR, deps, data generation
| +-- [data/scripts/...] # (Optional) Input files, configs
|-- solution/
| +-- solve.sh # Reference solution (not shown to agent)
+-- tests/
 |-- test.sh # Runs pytest, writes reward
 +-- test_outputs.py # Pytest verification tests

File-by-file guidance:
- instruction.md — Task description; state the goal, not the solution; use absolute paths
- task.toml — Fill in difficulty, category, and tags
- environment/Dockerfile — All input data must be present after docker build
- solution/solve.sh — Reference solution proving solvability (hidden from solving agent)
- tests/test_outputs.py — Pytest tests that verify the solution
## 4. Task Category:[CATEGORY_CONTENT]
## 5. Writing Style:[SCENARIO_CONTENT]
Critical rules for instruction.md:
- Describe the goal, not the solution. State what the agent should produce or achieve. Don’t explain the approach or give step-by-step guidance.
- Keep it concise. Match the scenario’s detail level. Look at the real examples for calibration.
- Use light formatting. Plain text and simple bullet points. The instruction should read naturally.
- Let the environment carry detail. Put complexity in environment files, not in the instruction text. The agent should explore to understand the full picture.
## 6. Difficulty Requirements:[DIFFICULTY_SECTION]
## 7. Dockerfile Best Practices
- NEVER use heredoc syntax (<< EOF) — causes Docker build failure. Use printf, echo, python3 -c, or COPY a file instead.
- NEVER COPY solution/ or tests/ into the image — they are mounted at runtime.
- Verify every COPY source exists under environment/ before finalizing.
- Pin dependency versions when stability matters: pip install numpy==1.24.0
- Delete setup scripts after execution: RUN bash setup.sh && rm setup.sh
- Required base: FROM python:3.11-slim, WORKDIR /app, install procps
## 8. Task Construction Approaches
- Implement from scratch — Agent builds something new given a goal
- Reverse engineer — Agent figures out what a binary, data file, or system does
- Build / compile — Agent compiles a real project from source, resolving dependencies
- Configure / deploy — Agent sets up a system or service
- Analyze / extract — Agent analyzes data or files to produce derived output
- Optimize — Working code provided, agent must make it faster/smaller
- Migrate / convert — Agent converts code or data between formats/languages/versions
- Debug / fix — Broken code provided, agent must find and fix the issue
## 9. Self-Validation (REQUIRED)
Before finishing, you MUST validate your task is solvable and consistent:
1. Execute your solution:bash /app/output/solution/solve.sh
2. Run tests to verify:pytest /app/output/tests/test_outputs.py -v
3. Verify consistency across all components:
- instruction ↔ solution: Does solve.sh actually accomplish what instruction.md asks?
- instruction ↔ environment: Does Dockerfile provide all files/tools mentioned in instruction?
- solution ↔ tests: Does solve.sh output match exactly what tests expect (paths, formats, values)?
- tests ↔ instruction: Every test traces back to a requirement in instruction.md, and every requirement has a corresponding test.
- No answer leakage: Re-read instruction.md as if you’re the solving agent — does it reveal the solution approach?
- Tests are robust: Tests verify behavior through execution (run code, check results), not just string matching or grep.
4. Verify difficulty requirements — Re-read the Difficulty section and verify your task meets each criterion.
5. If any check fails, fix and repeat from Step 1.
6. Clean up — Remove any output files created during validation.
## 10. Final Checklist
- All 6 required files present in /app/output/
- Solvability: solve.sh runs successfully and produces correct output
- Tests pass: pytest test_outputs.py -v shows all tests green
- Tests are general: any correct solution should pass, not just your specific solve.sh
- Output discoverable: the solving agent can figure out what to produce from instruction.md
- Self-contained: all paths, data, and dependencies exist inside the built container
- Consistency: instruction ↔ solution ↔ tests ↔ environment all correctly aligned
- Originality: task is genuinely different from examples
- Dockerfile: no heredoc syntax, valid packages, all COPY sources exist
- No answer leakage: instruction.md states the goal without revealing the solution approach
- Instruction style: follows the Writing Style rules — concise, natural, scenario-appropriate
- Difficulty: task genuinely meets [DIFFICULTY] criteria
```
