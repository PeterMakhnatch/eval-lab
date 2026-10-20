## TS01 `weak_check`: weak or missing check

**Question.** Does a wrong or partial solution get full reward because a requirement the instruction states is checked weakly, on too few cases, or not at all?

**Where to look.**
- Each stated requirement, next to the assertion that tests it.
- Fixed fixtures that could be special-cased.
- Checks of shape, existence or format that stand in for behaviour.
- Timing or measurement windows that an honest program can step around, such as untimed setup or masked spans.

**Useful moves (choose as needed).**
- Map requirements to the test lines with `task-info`. A requirement with no assertion is a lead, not a finding.
- Write a small wrong candidate and score it on the unmodified grader with `run-upstream-grader`. Options:
  - the reference with one requirement removed;
  - output with the right shape and wrong values;
  - a constant that matches the fixture.
- Use `diff` against the reference to show where the candidate is wrong.

**Sufficient evidence (confirmed).** All of the following:
- A wrong candidate is executed and gets full reward on the unmodified grader.
- The reference passes and a blank submission fails.
- The quoted requirement that the candidate violates.
- Output or a diff showing that the candidate really is wrong.

**Distinguishing check.** Use `probe` for an added ordinary functional assertion outside the fixed fixtures. It must pass on the reference and fail on the candidate. This diagnostic score is separate from the original upstream reward.

**Not this / false alarms.**
- The candidate passes by changing grader files → `grader_tamperable`; do not try this.
- The requirement is not stated anywhere → there is nothing to under-check.
- NaN, parser or comparison bugs → `grader_logic_bug`.
- Slack the instruction allows, such as a stated tolerance, is not weak.

**Stop / unknown.**
- Reference fails or blank passes → fix the controls first, and consider another class.
- The grader cannot be run → unknown.
- Mapping from source alone → suspected.
