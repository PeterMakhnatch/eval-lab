## TS08 `wrong_answer_key`: wrong answer key

**Question.** Is the expected answer, the reference output or the reference solution itself wrong?

**Where to look.**
- Expected values and fixtures.
- The reference's derivation.
- Domain facts in the instruction and the provided data that the key must agree with.

**Useful moves (choose as needed).**
- Derive the value independently, using:
  - the domain rule;
  - data provided in the package;
  - a computation run with `probe`.
- Quote the line that compares against the key.

**Sufficient evidence (confirmed).**
- An independent derivation showing the key is wrong.
- The comparing grader line.
- Ideally, confirmation from the maintainer.

**Distinguishing check.** Show the derivation step where the key and the package's own data or stated rule disagree. Do not rely on "the key looks odd".

**Not this / false alarms.**
- The key is right but too narrow → `overstrict_oracle`.
- Strong solvers disagreeing with the key is a trigger, not proof.
- A key that follows a reading the instruction leaves open → `unstated_requirement`.

**Stop / unknown.**
- The derivation needs sources outside the package or network access → suspected or unknown.
- Never guess a corrected value without a derivation.
