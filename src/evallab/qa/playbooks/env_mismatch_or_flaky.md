## TS09 `env_mismatch_or_flaky`: environment mismatch or flaky grading

This class also covers a broken environment.

**Question.** Does the same artifact score differently from run to run, or between the agent's environment and the grader's? Possible causes include versions, hardware path, clock, load, resource limits and nondeterminism.

**Where to look.**
- Dependency pins in the environment Dockerfile, compared with what the verifier installs (unpinned installs, a different interpreter).
- Unseeded randomness.
- Wall-clock thresholds.
- Resource limits in the task config.
- Network fetches at verification time.

**Useful moves (choose as needed).**
- Repeat `run-reference` a small number of times (two or three) within budget, and compare the results.
- Use `task-info` to cite the differing pin, seed or limit.

**Sufficient evidence (confirmed).**
- The same artifact scored at least twice with different results, or shown to pass in the agent's environment and fail in the grader's.
- The differing factor, named and cited.

**Distinguishing check.** Repeat the identical artifact with nothing changed. If the scores agree every time, flakiness is not confirmed. A cited version or limit difference stays suspected.

**Not this / false alarms.**
- The grader always rejects an answer for a code reason → `grader_logic_bug` or `overstrict_oracle`.
- Consistent reference failure is a lead for `infeasible_as_written`, not proof: the reference itself may be incorrect.
- A failure of our own runner (Docker missing, tool timeout, budget stop) is unknown, not a task defect.

**Stop / unknown.**
- Stop when the results differ and the factor is cited.
- The runtime is unavailable → unknown.
