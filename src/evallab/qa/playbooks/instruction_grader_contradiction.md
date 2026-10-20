## TS06 `instruction_grader_contradiction`: instruction contradicts grader

**Question.** Do the instruction, starter code or another test say X while the grader enforces not-X? This includes two tests that require incompatible behaviour.

**Where to look.**
- Stated thresholds and constants against the asserted ones.
- Starter docstrings and comments.
- Example outputs in the instruction.
- Tests that constrain the same behaviour.

**Useful moves (choose as needed).**
- Use `task-info` to quote both sides.
- Write a candidate with `probe` that follows the instruction exactly. Where possible, place it in the gap between the stated and the graded value. Then score it.

**Sufficient evidence (confirmed).**
- Both quotes side by side that cannot both hold.
- One of these, executed:
  - a solution that follows the instruction and is scored against it; or
  - a wrong-by-instruction solution that passes because the grader is looser than stated.

**Distinguishing check.** A candidate that satisfies the instruction but not the grader, or the reverse. The verdict follows the grader.

**Not this / false alarms.**
- The instruction is silent → `unstated_requirement`.
- Rounding or precision differences under which both sides can hold are not a contradiction.
- Two passages that read differently but agree on what they require are not a contradiction.

**Stop / unknown.**
- Quotes only → suspected.
- A candidate in the gap cannot be built → record why.
