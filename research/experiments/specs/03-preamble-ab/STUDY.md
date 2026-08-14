# Study 03 — Instruction-preamble A/B on event-summary

**Hypothesis.** Appending a short "read the contract, do not invent files"
preamble to the event-summary instruction changes Codex pass@3 relative to
the unmodified instruction. The only intended variable is the extra
instruction file.

**One variable.** Presence of
`research/experiments/preambles/brief-discipline.md` via Harbor's
`--extra-instruction-path`.

**Fixed.** `task=canary/event-summary`, `agent=codex`, `attempts=3`, docker.
The control cell is Study 01's event-summary spec. Do not submit a second
identical control.

**Why this task.** Event-summary has an exact schema contract
(`schema_version`, field names, percentile definition, output hygiene).
A preamble that tells the agent to satisfy the stated contract is a
plausible, small intervention. The task is also the cheapest canary.

**Harness gap (this study does not run).** Harbor 0.21.0 accepts
`--extra-instruction-path`. Two lab contracts do not:

- `ExperimentSpec` uses `extra="forbid"` and has no such field.
- `harbor_lab.runner.build_command` never forwards the flag.

RUNNER does not edit `src/`. Submitting two specs that the runner would
execute identically would be a fake A/B. The treatment arm is therefore
**not submitted**. The preamble file is staged so BUILDER can add
`extra_instruction_path: str | None` to `ExperimentSpec` and one
`--extra-instruction-path` pair in `build_command`.

**Policy once the field exists.** Control and treatment would both be
`canary` jobs at $2.50, attempts=3. Together $5.00. Still n=3, so a
significant A/B is not expected; the first executable version is a
direction check plus trajectory read, not a claim.

**Next spec this implies.** After the field lands, submit the treatment
arm only and pair it with Study 01's already-run control. If the control
has not run, submit both together.
