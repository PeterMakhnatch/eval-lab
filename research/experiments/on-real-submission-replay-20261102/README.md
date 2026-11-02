> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.

# Real Frontier Agent Submission Replay against Gold Graders (A8 & A8B)

## Overview
This experiment audits real frontier agent submissions and known-equivalent negative controls against certified Gold graders (A4B / Gold v1.1).
It evaluates whether original graders accepted submissions that Gold graders reject, measuring the empirical false-accept rate of benchmark graders on real frontier agent behavior and testing whether Gold graders falsely reject valid code.

## Inputs
- `gates/gold-v1.1-certified.json`: 8 certified Gold tasks (`gold_structural` or `gold_heldout`).
- `gates/submissions-inventory.json`: 482 usable submissions from historical trials across MiMo packages.
- `gates/gold-universe.json`: 50 executable candidate tasks designated for the Gold universe.

## Execution & Drivers
- `run_replay.py`: Full Modal execution driver evaluating reference controls, 9 real accepted submissions, and 24 verified AST negative controls, with multi-reader LLM adjudication (Gemini 3.8 Flash + Claude Sonnet).
- `test_replay.py`: Pytest suite validating receipt schema, Wilson interval computation, and control validity.
- `replay_audit.py`: Historical A8 static cross-join audit driver.

## Reproduction
```bash
uv run --extra laminar python research/experiments/on-real-submission-replay-20261102/run_replay.py
```

## Key Findings (A8B Certified Run)
- **Mandatory Control Invariant:** **8 / 8 = 100.0%** of reference solutions passed the Gold grader in the same session.
- **Real Submission Flip Rate:** **5 / 9 = 55.6% (95% Wilson CI: 26.7% – 81.1%)** of accepted real submissions are rejected by Gold graders.
- **Known-Equivalent Control Robustness:** **0 / 24 = 0.0% (95% Wilson CI: 0.0% – 13.8%)** false-reject rate on semantics-preserving AST rewrites of the reference solution across all 8 certified tasks.
- **Combined Valid Rejection Rate:** **5 / 33 = 15.2% (95% Wilson CI: 6.7% – 30.9%)**.
- **Adjudicated Flip Attribution (80% Reader Agreement):**
  - **Truly Defective Submissions (Original False-Accepts):** **2 / 5 = 40.0% (95% Wilson CI: 11.8% – 76.9%)**. Example: `format-code-task-000838` where a truncated syntax error slipped through the original grader.
  - **Gold Grader Over-Strictness:** **3 / 5 = 60.0% (95% Wilson CI: 23.1% – 88.2%)**. On `format-code-task-002391`, synthesized tests asserted reference-specific private helper methods on `VulnerabilityResult`, rejecting valid deduplication implementations.
