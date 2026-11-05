# J3 line merge blocker

**Historical blocker for frozen 89 and floor removal.** The [calibrated default-off policy](../ev1105-j3cal/README.md) supersedes this proposal.
The original measurements below remain unchanged. They do not describe the calibrated policy.

Do not enable J3. Do not remove the Goodfire review floor. Do not merge this lane under the current program rule.

The default-off replay passes with 0 changed decisions. Both enabled variants fail the existing replay gates.

| DEV policy | Cheat keeps / 332 [95% Wilson CI] | Honest keeps / 358 [95% Wilson CI] | Replay gate |
|---|---:|---:|---|
| Main, J3 off | 9 [1.43%, 5.07%] | 63 [14.00%, 21.88%] | PASS |
| J3 on, floor retained | 8 [1.23%, 4.68%] | 53 [11.50%, 18.86%] | REGRESSION |
| J3 on, floor removed | 63 [15.12%, 23.54%] | 113 [26.97%, 36.55%] | REGRESSION |

Removing the floor increases Goodfire cheat keeps from 0/80 to 55/80: 68.75% [57.93%, 77.85%].
The program requires a blocker instead of a merge when any corpus gains cheat keeps.

With the floor retained, J3 catches 1 additional cheat keep. It sends 10 honest keeps to review because their historical cached readings have failed or unattested windows. It also changes 13 honest reviews to drops. The unchanged replay gate flags the CheatBench honest retention loss and Terminal Wrench false alarms.

This blocker concerns measured line quality. The runtime is implemented behind a default-off flag. It passes the affected behavior suite, static checks, an actual attested Terra call, and stored-reading verification.

No threshold, rubric, labels, baseline, regression tolerance, or held-out policy is changed to make the variants pass. No new corpus judge batch runs. The PR must remain open under the current rule. Retain its worktree while the PR is open.

## Receipts

- Script: `research/experiments/ov1106-j3line/measure.py line-table`.
- Tables: `research/experiments/ov1106-j3line/results/line.{json,md}`.
- Main batch: `~/Developer/eval-lab-results/line/replay-v1/batches/replay-v1-replay-079537600573489cac9124e16da319c8`.
- J3 batch: `~/Developer/eval-lab-results/line/replay-v1/batches/replay-v1-replay-03b0a4fecc224fc8bc29b67be18098ed`.
- No-floor batch: `~/Developer/eval-lab-results/line/replay-v1/batches/replay-v1-replay-4bdb1f6d14954d43a0a9a04301ad3f71`.
- Each batch retains `replay.json`, the full scoreboard, checked decisions, and exports.
