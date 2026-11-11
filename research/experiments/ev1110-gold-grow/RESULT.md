# Gold grow result

PR #2054 admits 530 action-v2 row views. The registry contains 853 rows, including 460 binary rows.
These are protocol-specific row views, not independent tasks. Existing action-v1 labels remain unchanged.

## Protocol and custody

The 2 independent readers use `google-antigravity/gemini-3.8-flash`, at temperatures 0.0 and 0.2.
They use separate frozen prompts and cannot see the other answer or publisher label.
Positive admission requires matching mechanisms and verified executed-action citations from both readers.
Attempted and unjudged rows never become controls. Exact-substring citation validation remains unchanged.
R1 prompt SHA-256: `6b73a94b2f94bbbad25a2647faf026fb444e5093ee7e4d120c1c9311c86fba19`.
R2 prompt SHA-256: `1f62dca0614aee4b9428d6e8190dbdc053d040f3b44906d7da44672e4fbb0ccc`.

The full original report is preserved outside Git:
`/Users/petermakhnatch/Developer/eval-lab-results/gold/grow-v1/RESULT-full.md`.
SHA-256: `a5bd6bca1c287cd16b4927db9b6a7556c97f046f1eb211c04ea1b3f9dd87f0db`.
Full receipts remain under `~/Developer/eval-lab-results/gold/grow-v1/`.
Labels remain in `research/evidence/ev1110-gold-grow/action-v2-part-a.jsonl`.

## Counts by source, state, and protocol

| Source | Protocol | Rows | Executed-positive | Reviewed-control | Attempted | Unjudged |
|---|---|---:|---:|---:|---:|---:|
| goodfire-action-v1 | action-v1 | 150 | 11 | 70 | 23 | 46 |
| impossible-task-final | action-v1 | 173 | 43 | 32 | 0 | 98 |
| goodfire-action-v2 | action-v2 | 69 | 0 | 0 | 1 | 68 |
| terminal-wrench-action-v2 | action-v2 | 304 | 100 | 121 | 7 | 76 |
| cheatbench-action-v2 | action-v2 | 150 | 13 | 67 | 0 | 70 |
| c1c5-action-v2 | action-v2 | 7 | 1 | 2 | 0 | 4 |
| **Total** | separate protocols | **853** | **168** | **292** | **31** | **362** |

Action-v1 retains 323 rows and 156 binary rows. Action-v2 adds 530 rows and 304 binary rows.
Pre-A1 action-v2 counts are 101 positive, 123 control, 8 attempted, and 148 unjudged across 380 rows.
Post-A1 counts are 13 positive, 67 control, 0 attempted, and 70 unjudged across 150 rows.
The compact labels mark post-A1 rows with `amendment == "A1"` for separate analysis.

## Agreement and citations

| Phase | Valid pairs | Binary agreement | Mechanism agreement | Exact quotes | Resolved coordinates |
|---|---:|---:|---:|---:|---:|
| pre-A1 | 375 | 351/375 (93.6%) | 310/375 (82.7%) | 414/416 (99.52%) | 416/416 |
| post-A1 | 137 | 127/137 (92.7%) | 117/137 (85.4%) | 69/80 (86.25%) | 80/80 |
| total | 512 | 478/512 (93.4%) | 427/512 (83.4%) | 483/496 (97.38%) | 496/496 |

The single-attempt reproducibility gate passes 30/30 binary comparisons and verifies all 8 positive citations.
Part A has 10 malformed answers and 14 transport failures across 18 rows. Failed responses remain unjudged.
Both readers find no executed shortcut in 18/158 Terminal Wrench publisher-positive runs (11.4%).
This reader result does not prove that the publisher is wrong.

## A1 stop and limits

A1 changes the stopping rule after 380 candidates. Its committed amendment precedes continuation calls.
The study stops at 13/496 failed citations (2.62%), above A1's 2.0% limit.
All 496 coordinates resolve to tool calls; no fabricated event occurs.
The lane reads 530/599 candidates. The remaining C1-C5 13 and Sol/Astra 56 candidates receive no calls.
Part B does not start. No fresh cohort is admitted, registered, or scored.
The lane uses 1,060 Part A calls and 60 gate calls, without retries or prompt revisions.

The labels are model-reader consensus, not human review or proof of intent.
Part A reuses exposed DEV rows. Task, model, source, and constructed-variant confounds remain.
Missing rewards and outcome changes remain unknown. No causal reward-change total is claimed.
File-only controls and replay witnesses without original trajectory joins remain outside action-labelled Gold.
Cached scoring keeps protocols separate, preserves thresholds, and makes 0 new model calls.
The 21 A/B/C row values remain unchanged; full score receipts contain uncertainty and citation metrics.
The current combined measurement is `~/Developer/eval-lab-results/score/current/scoreboard.json`.
