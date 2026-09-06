---
source_type: internal
---

# Librarian questions: smallest borrowed stack for the loop (from Peter via researcher)

Peter's direction: assemble from standard parts, don't invent. Harbor stays as eval substrate; traces normalize to ATIF. For each pick below: ONE recommendation + why, with repo + license + maturity. No 50-paper maps — he wants the smallest thing that turns the loop.

## Q1 — Single task generator to start with (tool-use, verifier-backed)
Candidates: SWE-smith (infinite code tasks), Agent World Model (1K MCP tool envs), FuncBenchGen (hidden-DAG evals, BSD-3), SPADE (self-play curriculum, young). Which ONE first for tool-use tasks with automatic checkers, and what exactly does the lab write vs borrow for it?

## Q2 — Single trainer to start with
Candidates: TRL (SFTTrainer, easy) vs verl (scales, 23k★). For v1 = SFT on filtered traces, possibly GRPO later: which one, and what are the two adapter functions the lab must write (trace→records, verifier→reward)?

## Q3 — Trace→records conversion off the shelf?
Does anything (agent-data-protocol, Inspect dataframe APIs, TRL data utilities) already convert agent run logs into chat-template training records, or is that one converter the lab's to write? Point at the closest existing code.

## Constraints for your reply
- Concrete: name repos, functions, file paths where possible.
- Page back to the researcher pane when done. No repo writes needed.
