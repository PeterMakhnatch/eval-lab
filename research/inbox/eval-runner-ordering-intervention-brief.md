---
source_type: internal
---

# Brief — action-memory-v1 ordering intervention (Eval Runner)

From: Analyst. Assigned by Peter to Eval Runner. Analyst does not orchestrate this; page wK:p7 only if a finding needs interpretation.

## The finding this acts on (verified on main @ 93d2e7c1)
- 130 historical action-memory-v1 trials with verifier truth_digest: 72 pass / 58 fail.
- All 56 classified fails carry the same verifier reason: `incomplete_or_reordered_context_retrieval`.
- 40 of those 56 have `observed_reads == expected_reads` — the agent retrieved every required chunk and lost on ORDER only. 15 were short, 1 short-and-under.
- Zero failures at the value-binding gate (bound_value == latest_value).
- Verifier: `library/benchmarks/action-memory-v1/verifier.py` requires exact required-order reads, exactly one `execute_mutation`, as the LAST event.

## The intervention (smallest version)
One harness/prompt addition, no code change to the benchmark or verifier:
  "Before calling execute_mutation, list the chunk IDs you have read in the order you read them, and confirm they match the required order."
Nothing else changes. Same model, same tasks, same doses.

## Sample
The 40 complete-but-reordered fails. Locate them with:
  verifier/result.json has `expected_reads`, `observed_reads == expected_reads`, `reward == 0`.
Run each at k=3 (120 trials) on the already-qualified model (glm-5.3-flash per historical config; Gemini 3.7 Flash if a second lane is wanted).

## What to record per trial
- pass/fail and verifier reason
- observed read order vs required order (from benchmark-events.jsonl)
- whether the agent emitted the ordering statement, and whether it was correct

## Output
One table: baseline (0/40 by construction) vs intervention pass count, per dose (4k/16k/64k), with the 3 repeats shown separately — NOT averaged. Plus the per-trial CSV. Page wK:p7 with the path when done; Analyst reads the trajectories from there.

## Boundaries
- Historical corpus is calibration-only; this run is a calibration comparison, not governed measurement. Say so in the output.
- Do not touch verifier.py or the contract. If the intervention "works" by changing what is scored, that is a different experiment.
- If the model has no credential/route, stop and report; do not substitute a different model silently.
