---
status: living
audience: [builder, analyst, runner]
---

# Hardening loop on the 33 v7.1 held-out graders: 2026-10-19

This is Track A of the 2026-10-19 deep plan. The loop is breaker, then repair, then re-break, run on the 33 v7.1 MiMo held-out tasks. The question is whether new wrong submissions run out round by round, and whether the repairs start rejecting correct passes.

All evidence is in [`research/evidence/hardening-loop-20261019.tar.gz`](../../evidence/hardening-loop-20261019.tar.gz). Its `MANIFEST.json` lists the sha256 of every member. The capsule holds:

- the producers;
- the inputs, with package digests;
- every attempt record and compiled candidate patch;
- the probe logs and qualification rows;
- the canonical-pass regrade observations;
- the LLM and Modal ledgers.

Container trial trees and prompts are left out. They can be rebuilt from the inputs and producers.

## Protocol

- **Breaker.** QA Task's S2a ordinary-program seeker (`MIMO_SEMANTIC_PROMPT`, `mimo_semantic_edit_patch`), graded with `HarborBackend.grade_shortcut`. Every reward-1 program is replayed in a fresh container (S3).
  - K≤3 attempts per task.
  - The seeker sees the instruction, the grader's visible unit tests and the pristine source. It never sees the reference.
  - Seeker models: `openrouter-metered/openai/gpt-oss-120b` (rounds 2–4) and `openrouter-metered/z-ai/glm-5.3-flash` (round 5 with K≤3; round 6 with K=1).
- **Wrongness is never taken from reward.** For every program that passes S2a and S3, I write a requirement test myself; the model never writes it. The test runs on the candidate, the reference and pristine in one native container. A program counts as verified wrong only when the candidate fails the test and the reference passes it.
- **Repair.** Append-only `derive_repair_variant`: the original `tests/test.patch` prefix and the original command are kept, and one new test file is appended. A repair qualifies when, natively:
  - the reference passes 3/3 times;
  - the do-nothing patch (nop) fails;
  - every wrong program from every round fails, including EnvCheck's archived reproducers;
  - every admitted correct alternate passes.
- **Over-tightening.** Three checks:
  - the canonical real passes from `regrade-v7.json`, replayed natively (`heldout-v7/native.py`) on every repaired grader;
  - one fresh "solve it correctly" attempt per repaired task, graded on v7.1 and on the repair;
  - the admitted correct alternates.

## Results

| Round | Graders attacked | Verified wrong (tasks) | Repairs qualified | Canonical real passes rejected | LLM cost (reported / reserved) |
|---|---:|---:|---:|---|---|
| 2 (GPT-OSS) | 32 of 33 v7.1 | 7 | 7/7 | 1/17 (adjudicated: wrong pass) | $0.064 / $0.337 |
| 3 (GPT-OSS) | 7 round-2 repairs | 1 | 1/1 | 0/0 | $0.013 / $0.069 |
| 4 (GPT-OSS) | 1 round-3 repair | 0 | – | – | $0.005 / $0.018 |
| 5 (GLM, K≤3) | 7 current repairs | 4 (1 withdrawn) | 3/3 kept | 1/10: 000161, **over-tightening, repair withdrawn**; 001248 ×5 and 002451 ×3: 0 new | $0.019 / $0.121 |
| 6 (GLM, K=1) | 24 of 25 unrepaired v7.1 | 4 | 3/4 (000657: reference 2/3) | 2/9: 000371 ×2 (adjudicated: wrong passes, contested) | $0.054 / $0.297 |

How to read the table:

- **Convergence depends on the seeker.** With the GPT-OSS seeker, new verified wrong programs per round went 7 → 1 → 0. A second model family then found 3 more on the already-repaired graders and 4 on graders that GPT-OSS had failed to break. A zero in a late round measures that seeker and budget; it does not show the grader is exhausted.
- **Withdrawn repair.** The round-5 repair for 000161 (flag `split` calls that pass `maxsplit`) qualified on every gate. Replaying the canonical passes then showed it rejected one v7 real pass, and the instruction never mentions `maxsplit`. I withdrew it, and 000161 stays on its round-2 repair.
- **Fresh correct solves:** 6 were accepted by v7.1, and all 6 were also accepted by the repaired graders.
- **Admitted correct alternates:** 1 exists (000371), and it passes its repair.
- **000657 is a grader defect, not a repair defect.** On the unrepaired v7.1 grader the reference also passes only 2/3 times. The failing test is the original timing assertion `test_mixed_states` (`runtime > 0.3 s` with randomized backoff), so this is a flaky original grader.
- **Not attackable:** 002335. Its reference only adds new files, so there is no existing production file to edit. In round 6, 000878 asked for more source context and was not graded.

Repaired packages are for calibration only: `scoring_allowed=false` and `training_allowed=false` until the Verifier qualifies a v7.N manifest. The bridge write-up is `research-context/bridge/2026-10-19-deep/hardening.md`, with round files `hardening-round-N.json` and `repairs-round-N.json`.
