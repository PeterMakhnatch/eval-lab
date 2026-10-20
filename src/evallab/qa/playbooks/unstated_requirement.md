## TS05 `unstated_requirement`: unstated requirement

This class also covers hidden rules, ambiguous instructions and withheld information.

**Question.** Does the grader enforce a rule, constant, format, file location or interpretation that the instruction never states, only hints at, or leaves open between several readings?

**Where to look.**
- Every grader constant, threshold, estimator, output format and graded path.
- Then search the instruction, the starter code and the provided specs for each one.

**Useful moves (choose as needed).**
- Use `task-info` to quote the enforcing line and the instruction passage, or to show the instruction is silent on the point.
- List the readings a competent solver could reasonably take.
- Change the reference to follow one other reasonable reading. Score it with `run-upstream-grader`; use per-test output to isolate the rejection and `diff` to show the alternate implementation.

**Sufficient evidence (confirmed).** All three:
- The instruction quote, or its silence.
- The enforcing grader line.
- An executed candidate that follows a reasonable reading and fails only that check.

**Distinguishing check.** The alternate-reading candidate must pass every other check. If it fails others too, the failure is not isolated.

**Not this / false alarms.**
- The instruction says the opposite → `instruction_grader_contradiction`.
- The behaviour is agreed and only the form is narrow → `overstrict_oracle`.
- The rule is stated anywhere the solver can see it, including starter docstrings and provided files → not unstated.
- Conventions a competent practitioner would assume are not unstated. Argue explicitly why the reading is reasonable.

**Stop / unknown.**
- With quotes but no executed alternate reading → suspected.
- If no reasonable alternate reading can be stated, rule the class out.
