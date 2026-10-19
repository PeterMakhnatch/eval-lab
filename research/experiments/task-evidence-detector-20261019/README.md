# Task-evidence wrong-pass detector (2026-10-19)

Question: trace-only detectors miss MiMo wrong passes. Does task evidence work instead,
specifically "flag a pass if its task is a known weak-test task"?

The rules were declared in research-context `bridge/2026-10-19-deep/transfer-matrix.md`
(07:33 ET) before scoring. `run.py` was scored once; rerunning it reproduces
`results.json` byte for byte. There are no model calls and no new runs.

| key (v6 passes) | detector | wrong_pass recall | real_pass false alarms | flagged tasks |
|---|---|---|---|---|
| MiMo70 (6 / 64) | D1: weak in atlas v2.1 ∪ sample100 | 0/6 [0, 39%] | 9/64 = 14% [8, 25] | 3 |
| | D1-v1: weak in v1 seed key (2026-10-15) | 0/6 | 9/64 | 3 |
| | D2: D1 and witness file untouched | 0/6 | 0/64 | 0 |
| window-3 (4 / 17) | D1 | 0/4 [0, 49%] | 10/17 = 59% [36, 78] | 4 |
| | D1-v1 | 0/4 | 7/17 | 3 |
| | D2 | 0/4 | 0/17 | 0 |
| context: v7 grader is a repair | MiMo70 | 6/6 | 18/64 | 11 |
| | window-3 | 4/4 | 11/17 | 7 |

- **Why recall is 0:** all five wrong-pass tasks (000143, 000149, 000783, 002371, 002915) are
  `not_established` in atlas v2.1, each with `unknowns: line_pass_not_run_or_not_reported`.
  The atlas never ran line mutants on them, so this is a coverage gap, not evidence that
  they are sound. The atlas-weak tasks that do appear (000089, 000371, 000657, 001686) produced
  only real passes. Sample100 shares no task with either key.
- **D2 behaves as intended but cannot be tested for recall:** every one of the 19 real passes
  on atlas-weak tasks edited the witness file, so D2 clears all of them. With no positives
  on a weak task, its recall is untestable here.
- **The context row is label construction, not a detector.** A wrong pass can only arise
  where the v7 grader differs from v6, so "the task has a repaired grader" catches every
  wrong pass by definition.
- The edited-file sets come from the frozen `evallab.action_features` write events on the
  actions view (matched to repo-relative paths by suffix), not from the graded diff.
