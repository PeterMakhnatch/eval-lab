## TS02 `grader_logic_bug`: grader logic bug

**Question.** Does the grader's own code do something its author plainly did not intend, so the score does not follow from the checks it means to run?

**Where to look.**
- Tolerance comparisons such as `abs(a-b) > t`.
- Handling of NaN, None or empty values.
- Normalisers for strings and numbers (separators, case, whitespace).
- Parsers for logs or results.
- Aggregation and weighting.
- Exception paths, exit-code handling, and early returns.

**Useful moves (choose as needed).**
- Read the comparison and parsing lines with `task-info`.
- Build a minimal candidate with `probe` that hits the suspect edge: NaN or None in a numeric field, an empty result, unicode or a BOM, or numeric separators.
- Run a matching control input that the line handles correctly.

**Sufficient evidence (confirmed).** All three:
- The grader line, quoted.
- An executed edge input that produces the unintended result: a wrong answer passes, a correct answer scores 0, or a failed test is parsed as passed.
- A control input that behaves as intended.

**Distinguishing check.** Two runs that are identical except for the suspect value. Only the edge value flips the verdict.

**Not this / false alarms.**
- The code works as written but tests too little → `weak_check`.
- The code works but expects the wrong value → `wrong_answer_key`.
- The result differs only between environments → `env_mismatch_or_flaky`.
- Behaviour the instruction explicitly allows is not a bug.

**Stop / unknown.**
- The grading code is outside the package → unknown.
- The edge cannot be reached through any ordinary submission → suspected, with the line cited and "runtime behaviour unverified".
