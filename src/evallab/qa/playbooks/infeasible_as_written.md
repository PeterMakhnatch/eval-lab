## TS10 `infeasible_as_written`: infeasible as written

**Question.** Is there no correct solution that can get full reward as the task stands? For example, the reference fails, the inputs make the goal impossible, or a defect elsewhere blocks every route.

**Where to look.**
- The `run-reference` result.
- The check it fails.
- Requirements that conflict with each other or with the provided inputs.

**Useful moves (choose as needed).**
- Run `run-reference` first.
- If it fails, read the failing check and the input it uses with `task-info`.
- Identify the smallest controlled change that would make the task passable. Cite it; do not apply it to grading files.

**Sufficient evidence (confirmed).**
- One of:
  - the reference or an expert solution fails in a clean run (receipt);
  - a proof that the requirements cannot all hold.
- A controlled change that makes the task passable.
- If that change was cited but not executed, say so.

**Distinguishing check.** Rerun the reference once. A consistent failure on the same check points here. A varying result points to `env_mismatch_or_flaky`.

**Not this / false alarms.**
- Hard is not infeasible. Zero passes across solvers is only a trigger.
- A reference failure caused by our runner or infrastructure is unknown.
- A wrong key that every correct answer misses → `wrong_answer_key`.

**Stop / unknown.**
- The reference cannot be run → unknown.
- The reference passes → rule this class out.
