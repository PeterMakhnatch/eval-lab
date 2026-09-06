---
source_type: internal
---

# Tutor questions: train-on-traces loop, plain validation (from Peter via researcher)

Peter's confusion, verbatim spirit: what does "train on traces" concretely require? Is filtered imitation enough for loop v1, or is verifier-reward RL needed from day one? He wants the minimal sound version, not a 50-paper map.

## Q1 — Minimal sound "train on traces" for tool-use
Proposed v1: run Harbor tool tasks on an open model → keep only traces whose verifier says success (+ duplicate/order hygiene facts) → reformat to prompt/response pairs → SFT (off-shelf trainer) → re-evaluate on held-out tasks. Is this methodologically enough to *prove the loop turns* (measure a lift), or does v1 already need RL/verifier-reward training to be a real test? If SFT suffices for v1, say so plainly.

## Q2 — Filter validity
Observed: duplicate-handle-request rate is 0 in all 28 passing memory traces vs mean 11.1 in 25 fails (Class-B, Darwin calibration-only, n≈53). Valid selection signal for SFT, or confounded (e.g., measures task length/loopiness, not competence)? What control would decide?

## Q3 — Falsifiers per stage
For each stage (trace quality → SFT lift → synthetic-task lift → RL lift): what single result would kill that stage? Peter wants kill criteria, not open-ended exploration.

## Constraints for your reply
- Plain words first, formalism second. Peter is drowning in jargon; lead with the concrete.
- Page back to the researcher pane when done. No repo writes needed.
