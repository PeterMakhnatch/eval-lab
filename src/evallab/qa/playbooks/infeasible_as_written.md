## TS10 `infeasible_as_written`: infeasible as written

**Question.** Do the requirements or supplied inputs make every correct solution fail? A failing reference is only a trigger: the reference may itself be wrong.

**Where to look.**
- The `run-reference` result.
- The check it fails.
- Requirements that conflict with each other or with the provided inputs.

**Useful moves (choose as needed).**
- Run `run-reference` first.
- If it fails, read the failing check and the input it uses with `task-info`.
- Identify the smallest controlled change that would make the task passable. Cite it; do not apply it to grading files.

**Sufficient evidence (confirmed).**
- A cited contradiction or input constraint that rules out every solution satisfying the instruction.
- A clean, usable execution showing the corresponding rejection, with independently justified correct behavior; a reference failure by itself is insufficient.
- A controlled, task-specification-level correction that removes the contradiction. If it was only cited rather than executed, retain the finding as suspected.

**Distinguishing check.** Rerun the same justified artifact to separate a stable rejection from flakiness. Investigate reference correctness before concluding that the task is impossible.

**Not this / false alarms.**
- Hard is not infeasible. Zero passes across solvers is only a trigger.
- A reference failure caused by our runner or infrastructure is unknown.
- A wrong key that every correct answer misses → `wrong_answer_key`.

**Stop / unknown.**
- The reference cannot be run → unknown.
- The reference passes → rule this class out.
