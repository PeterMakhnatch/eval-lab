## TS03 `answer_reachable`: answer reachable from the sandbox

**Question.** Could the solver read the answer, reference solution, hidden tests or oracle, graded data, or a future fix? Routes include files in its image, git history, network access, or the prompt itself.

**Where to look (configuration review).**
- `COPY`/`ADD` lines in the environment Dockerfile and the build context.
- Task config for mounts, network or internet access, and which directories reach the agent. Check this against the verification-time upload of tests and solution.
- Copied `.git` directories.
- Data duplicated between the environment and the graded expectations.
- Instruction text that contains the solution.

**Useful moves (choose as needed).**
- Use `task-info` to read the inventory and the cited config lines.
- Use `diff` on package files to show that an agent-visible file matches the graded answer, or contains it.

**Sufficient evidence.** This class is *confirmed* only by a working path in a fresh container. Do not reproduce that here. Record it as *suspected*, with:
- the config line that places the artifact in the agent's environment, or that enables egress;
- the file it exposes;
- where possible, a package-level `diff` or byte match against the graded answer;
- "runtime visibility unverified".

**Distinguishing check.** Does the exposed copy actually match what the grader compares against? Use `diff` on the package files. A name collision alone is not a leak.

**Not this / false alarms.**
- Tests or solution uploaded only for verification, after the agent finishes, are not reachable.
- Public inputs that the instruction intends the solver to use are not a leak.
- Memorisation from pretraining is outside this taxonomy.
- Editing the grader rather than reading the answer → `grader_tamperable`.

**Safe remediation intent.**
- Remove or seal the source.
- Supply verifier assets only at verification time.
- Strip git objects.
- Block egress.

**Stop / unknown.**
- Stop once the exposing line and the exposed file are cited. Never try to read the answer from inside a sandbox.
- Base image or build steps outside the package → unknown.
