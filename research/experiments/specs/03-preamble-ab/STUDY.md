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

**Harness gap — closed.** This study was blocked because the runner could
not express the one variable. That is no longer true:

- `ExperimentSpec.extra_instruction_path` exists (`src/evallab/schemas/__init__.py`).
- `build_command` forwards it as `--extra-instruction-path`
  (`src/evallab/execution_contracts.py`).

`research/experiments/specs/03-preamble-ab/treatment.intended.json` now
carries the preamble path and the model, so it is the treatment arm rather
than a second copy of the control. It is **prepared, not submitted**.

**Policy.** Control and treatment are both `canary` jobs at $2.50,
attempts=3. Together $5.00. Still n=3, so a significant A/B is not
expected; the first executable version is a direction check plus trajectory
read, not a claim.

**Next spec this implies.** Submit the treatment arm only and pair it with
Study 01's already-run control. Do not submit a second identical control.

## 2026-08-15 PROGRAM reconciliation

Control cell has now run: `runs/canary-event-summary-codex-20260815/`
3/3 `reward=1.0`. Still not submitted.

## 2026-09-10 correction

The "harness gap" recorded above outlived the gap itself and was still
telling readers to add a field that already existed. Corrected, along with
the matching `blocker` in `research/experiments/PROGRAM.json`.

The remaining blocker is authorisation, not code: the treatment arm is
billable `codex` and needs a named human authorisation.

The treatment spec pins `model=gpt-5.6-terra` because this entry's
`fixed_elicitation` names it and a comparison should pin its model rather
than inherit the adapter default. The control cell did **not** pin one: it
ran on the adapter default (`DEFAULT_AGENT_MODELS` in
`src/evallab/credentials.py`, applied in `src/evallab/queue.py`), which
resolves to `gpt-5.6-terra` for codex. The two agree today.

