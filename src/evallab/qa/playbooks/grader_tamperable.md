## TS04 `grader_tamperable`: grader tamperable by the agent

**Question.** Does grading trust files, processes, binaries or outputs that agent code controls? If so, agent code could change what the grader measures or reports.

**Where to look (configuration review).**
- Verifier commands: is the submission imported or executed inside the verifier process?
- Does the verifier run in the same container or user after the agent finishes?
- Test runners and interpreters resolved from an agent-writable `PATH`.
- Result or success files under agent-writable directories.
- Timing or memory measured inside the agent's own process.
- Does the verifier restore every test and infrastructure file, such as `conftest.py`, or only some of them?
- The agent's user: is it root?
- File ownership and permissions on grading assets.

**Useful moves (choose as needed).**
- Use `task-info` on the verifier script, the Dockerfile `USER` and permission lines, and the task config.
- A `run-reference` receipt can show which files or processes the verifier actually uses. You must not modify them.

**Sufficient evidence.** This class is *confirmed* only by an executed submission that flips the verdict without solving the task. Do not build or run that here. Record it as *suspected*. For each trust point give:
- the line that trusts it;
- the agent-writable thing it trusts;
- the line or setting that makes it writable (user, permission, path, or shared process);
- "runtime behaviour unverified".

**Distinguishing check.** Is the trusted path or process actually writable or controllable by the agent? Cite the ownership or user line. If the agent cannot reach it, there is no finding.

**Not this / false alarms.**
- Only reading an answer → `answer_reachable`.
- The grader is untouched and simply checks too little → `weak_check`.
- Untimed setup or masked windows that an honest program can step around without touching the measurement → `weak_check`.
- Root access on its own is a contributing condition. It becomes a finding only together with a cited grading surface that root can write.

**Safe remediation intent.** Separate the verifier:
- its own process and uid;
- restored tests and runner binaries;
- a clean image;
- measurement outside the agent's process.

**Stop / unknown.**
- Stop when the trust points are cited.
- Verifier harness code outside the package → unknown.
- Never write to grading files, runners or result markers, and never forge output.
