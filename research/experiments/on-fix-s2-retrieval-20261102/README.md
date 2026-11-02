# FIX-S2-RETRIEVAL: Restored Stage-2 Evidence Retrieval

Restores Stage-2 evidence retrieval in `src/evallab/autoreview_monitor.py`:
- Prioritizes L0-hit steps first (sensitive reads, environment probes, test key reads)
- Prioritizes Stage-1 cited steps second
- Prioritizes mutations to target surfaces third, then other mutations
- Caps candidate steps at `max_steps=6` (sized from stored p90 step count of 6 steps)
- Caps total prompt at `max_total_chars=45_000` (sized from stored p90 prompt size of 43,332 chars)
- Truncates long step outputs per step rather than dropping steps when exceeding character budget
- Retains today's rubric

## Verification
- Cohort A (46 positive cheats, 2 clean runs):
  - Repeat 1: 29 / 46 TP (63.0%), 1 / 2 FP
  - Repeat 2: 30 / 46 TP (65.2%), 1 / 2 FP
  - Indistinguishable from stored production prompt (Arm B1: 31/46 and 30/46, McNemar p=0.6875 and 1.0000)
  - Significant recovery over today's regressed code (+5 to +7 detections, McNemar p=0.0156 to 0.0312)
- End-to-end 136-case report split:
  - Repeat 1: Recall 21/67 (31.3%) at FA 2/69 (2.90%)
  - Repeat 2: Recall 22/67 (32.8%) at FA 2/69 (2.90%)
  - Fixed alert (>= 50): 20/67 (29.9%) and 21/67 (31.3%) at FA 2/69 (2.90%)
