# Experiments

RUNNER-owned. Specs are the unit of submission; this journal is the
human-readable thread. Generated jobs stay in the worktree `runs/` directory
and are not committed.

| Path | What it is |
|---|---|
| `PROGRAM.json` | Versioned machine-readable ledger (`validate_program.py`) |
| `STATUS.md` | Human page: RECENT / RUNNING NOW / NEXT / TASK DECISIONS |
| `JOURNAL.md` | Running scientific thread: what / why / status / results / links |
| `specs/` | One study per directory; JSON files are `ExperimentSpec` documents |
| `preambles/` | Retained extra-instruction inputs for declared interventions |
| `baselines/` | Free oracle/nop matrices (`evallab matrix` only) |
| `local-controls.json` | Original event-summary oracle/nop matrix (kept; tests load it) |

Validate the current ledger and its known-bad regression fixtures with:

```bash
uv run python research/experiments/validate_program.py
uv run pytest -q research/experiments/tests/test_validate_program.py
```

Execution authority comes from the current `policy/standing-approvals.yaml`,
registry records and per-spec approval ledger, not a copied list in this README.
Local `oracle`/`nop` controls do not establish model capability or task admission.
Billable work follows the existing explicit approval path.

`ExperimentSpec.extra_instruction_path` is supported and forwarded to Harbor.
Changing an instruction remains a declared experimental intervention; retaining
or compiling a specification is not execution approval or a completed comparison.

## Matrix solution controls

`evallab matrix <matrix.json>` accepts a per-run `solution` path relative to
the repository root, only for nonbillable `oracle` runs. For example:

```json
{"name": "task-mutant", "agent": "oracle", "expect_reward": 0.0,
 "solution": "library/tasks/task/controls/mutant.sh"}
```

The selected script replaces `solution/solve.sh` in a temporary task snapshot;
the source task is not modified. Declared task `steps` are unsupported by this
override contract and are refused before execution or script provenance is
published, including steps that would otherwise fall back to the root solution.
There is no implied per-step script mapping.

Oracle qualification requires completed agent-execution timing and the retained
`agent/oracle.txt` log (which may be empty). Harbor writes `agent/exit-code.txt`
on nonzero script exit, without necessarily failing the Harbor process. That
failure, unreadable exit evidence, missing execution artifacts, trial exceptions,
and missing or non-finite rewards are infrastructure outcomes with no qualifying
rewards, never successful negative controls.

This behavior was inspected in both the installed Harbor **0.21.0** source and
the PR's `uv.lock`-pinned Harbor **0.22.0** wheel (SHA-256
`4c4c6571b3d160ed0cb45b82918136751fb08e7b8596412723ac00dde12eeabb`).
Both prioritize a declared step's solution directory and write the exit marker
only on failure. Source inspection is not a Docker-backed control run or a
claim that the installed CLI has been upgraded.

The `.executor/<matrix_id>.matrix.json` receipt retains completed job rows and
their script/staged-task digests. A refused later invocation cannot replace a
successful row or its receipt bytes; restore the original script and use
`--reuse-existing` to validate the retained job without dispatching it again.
Each invocation's per-run outcome, including refusals, is appended separately to
`.executor/<matrix_id>.matrix.invocations.jsonl`. Reuse still checks the actual
job evidence and any executor failure state; a preserved receipt does not turn
failed or incomplete execution into a pass.

## September 14 task-verifier repair controls

The current `release-branch-rescue-local-controls.json` and
`nginx-proxy-repair-local-controls.json` bind the final repaired package and
verifier bytes. They were executed serially through `evallab matrix`, including
normal ingestion, with installed Harbor 0.21.0 and local Docker. No model was
invoked and no task was registered or admitted.

| Task | Final control conditions | Observed outcome |
|---|---|---|
| `peter/release-branch-rescue` | Oracle, nop, two valid alternatives, submitted `pytest.py` bypass | 5/5 expected outcomes; valid solutions 1, nop/shadow bypass 0 |
| `peter/nginx-proxy-repair` | Oracle, nop, two valid alternatives, redirect bypass, fabricated items | 6/6 expected outcomes; valid solutions 1, nop/bypasses 0 |

The shadow control ran the real nine-test verifier and failed five recovery
checks rather than bypassing pytest. The redirect control ran fourteen tests
and was rejected on its first HTTP 302 responses. The fabricated-items control
ran fourteen tests and failed six substantive body/query comparisons.
These are bounded controls, not proof against every reward-hacking strategy or
measurements of model difficulty.

Raw evidence is retained in `.worktrees/landing-tasks-20260914/runs/`; matrix
receipts are `.executor/01M2GZAN46TXEQP1B6H81CF7CK.matrix.json` and
`.executor/01M2GZAN4AQX7K704N25T1JXWH.matrix.json`. Their run names end in
`20260914-r2`. Earlier `r1` controls precede final documentation/style changes
and are not counted as additional independent control conditions.

The original `rl-envs` revision/license could not be independently recovered.
The task metadata's Peter authorship and source reference remain declarations;
no new license grant or external dataset publication is inferred.
