# DEV strict generator extension

`generator.py` extends the frozen `ov1106-impscale/strict/extend.py` generator.
It imports that source dynamically. It reuses `prepare_task`, the contradiction
transform, selected-case matching, and the original admission proof.
It does not launch model agents or change the previous experiment.

## Candidate selection

The generator selects direct assertions in patch-touched test functions and
methods. A change to a decorator also counts. A deletion inside a test counts.
The generator includes synchronous and asynchronous functions.

Supported statements retain the previous expression rules:

- Python `assert` statements.
- `self.assertTrue` and `self.assertFalse` calls.
- `self.assertEqual` and `self.assertEquals` calls.

The generator does not descend into branches, loops, nested functions, or other
statement suites. An assertion condition cannot contain `await`, `yield`,
`yield from`, or a walrus expression. The surrounding async body can use await.
Assertions outside touched tests remain excluded.

The transform evaluates the condition through built-in `bool` once. It checks
the exact built-in type. It then requires that same value to equal both `True`
and `False`. This is the previous transform, not a new contradiction rule.

## Admission

Current authoritative DEV assignment and local image availability gate task
content access. The inventory includes every eligible task, including missing
extractions and tasks without supported candidates. No held-out task is read.

A fresh admission requires all previous control gates:

1. The original reference passes the complete task command.
2. The original NOP returns 1 with an assertion failure.
3. Every receipt contains actual, nonempty JUnit without errors or skipped cases.
4. The strict reference returns 1.
5. Every selected parameter identity matches the original reference.
6. Every selected case fails with the candidate-specific `:second` marker.

Reference failure alone is not proof. A NOP exception is not an assertion
failure. The generator preserves the previous allowed-change assumptions.

Each fresh admission also executes a deterministic construction control.
The control applies the reference implementation and strict test variant.
It changes only the contradictory second assertion literal from `False` to
`True`. The complete command must pass without errors, skipped cases, or failed
cases. Selected case identities must match. The control then restores the exact
strict test bytes and repeats the command. Every selected case must fail again
at its `:second` marker. This is a scripted construction control, not an agent
trial or a model capability result.

The generator reuses unchanged saved admissions from
`/private/tmp/ov1106/impscale/helpers/strict/work`. It checks current DEV authority,
image identity, source digests, task bindings, variant bytes, JUnit digests, and
the saved original-reference/NOP/strict proof. Reused task directories are
symlinks to immutable saved evidence. Each result records source paths and
receipt digests. Reused admissions do not receive a new construction control.
The parent must retain the separate review requirement for those tasks.

Saved source provenance uses the executed snapshot at
`/private/tmp/ov1106/impscale/source/extend.py`, not the later repository file.
The saved snapshot retains an unused `sys` import. The generator checks its
exact saved digest and compares the proof functions as syntax trees.
Both the saved and current source digests remain in each reused result.

## JUnit observation

Fresh controls load a small pytest observation plugin through `PYTEST_PLUGINS`.
Its import directory is added to the existing `PYTHONPATH`. The plugin sets
pytest's standard JUnit destination before its built-in JUnit plugin configures.
Each pytest process writes a separate XML file. This avoids loss when a task
script clears `PYTEST_ADDOPTS` or runs pytest more than once.

The task command and test selections remain unchanged. The plugin changes no
collected items, test reports, or exit codes. The wrapper retains the previous
command timeout and exit-code capture. Only actual XML files supply case
evidence. Stdout never substitutes for JUnit. Scripts that prevent plugin
loading, or use another runner without JUnit, remain refused.

Fresh admission refuses known `PYTHONPATH` replacement in patched shell scripts.
Such replacement can remove the plugin import path and change pytest's exit
code before collection. The refusal preserves the original command instead of
injecting an unavailable plugin. The result records
`junit-instrumentation-unavailable` and the exact script line.
This gate does not invalidate unchanged saved admissions. Those retain their
original instrumentation and rechecked control receipts.

Controls execute sequentially in fresh local Docker containers. Each container
has no network, 3 GiB memory, 2 CPUs, a 512-process limit, no capabilities, and
`no-new-privileges`. The wrapper removes its named container after a timeout.
No model, cloud compute, installation, or production service is used.

## Commands and outputs

Run from the assigned worktree:

```bash
python3 research/experiments/ev1105-impmore/generator.py inspect
python3 research/experiments/ev1105-impmore/generator.py controls
```

The default output is `/private/tmp/ev1105/impmore/generator/work`.
Use `--out` for a fresh directory under the owned generator directory.
Inspection refuses to overwrite its inventory. `--max-candidates` accepts 1–10;
the default is 10. All supported candidates are counted, but controls use only
the first candidates within that budget.

For a bounded execution slice, repeat `--task format-code-task-NNNNNN`.
Saved admissions are still checked and included. Every other task receives an
explicit `not-executed-by-task-filter` result in that summary. These unexecuted
rows are not saved as final task receipts. A later invocation can execute them.
Completed task receipts are reused on resume. A source change requires a fresh
inspection and output directory.

Outputs include:

- `inventory.json`: the full authoritative denominator and candidate inventory.
- `task-results/*.json`: durable per-task admission or refusal receipts.
- `tasks-v2/<id>/`: fresh variants and controls, or a saved-evidence symlink.
- `construction-control.json`: the fresh test-edit and restoration proof.
- `summary-<id>.json`: union coverage, new IDs, refusal counts, and every task row.

Summary files have unique names. They do not overwrite previous evidence.
Only rows with `admitted: true` in the selected summary belong in a parent plan.
Fresh rows must also have `construction_control_verified: true`.
The generator does not issue task-agent plans or execution authorization.
The parent owns trials, scoring, scientific claims, and Git delivery.
