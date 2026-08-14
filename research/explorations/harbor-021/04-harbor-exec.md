# harbor exec — compile paths into a job

## What it is

`harbor exec` (experimental) compiles file paths + an instruction into a
Harbor task (auto-inferred artifacts, optional `--task-template`) and runs a
job. Inverse of `harbor run --path <existing-task>`. Useful for one-off
"here is a file, do this" jobs without checking in a task directory.

## Demo

```bash
bash explorations/harbor-021/demos/run-exec.sh
```

Compiles `demos/exec-input/hello.txt` with instruction "Copy hello.txt to
`/app/hello-out.txt`" and a tiny oracle `solution/solve.sh` template. Agent:
`oracle`. Jobs land under worktree `runs/`.

Observed (2026-08-13):

`--print-config` resolved one map task, artifact `/app/hello-out.txt`,
template + path upload. Then:

```
Warning: `harbor exec` is experimental; flags and behavior may change.
  1/1 Mean: 1.000
Map Results  adhoc • oracle
Trials 1  Exceptions 0  Mean 1.000
Reward 1.0  Count 1
Map tasks written to .../demos/exec-compiled
Map job written to .../runs/exec-oracle-demo
```

Compiled tree contains `instruction.md`, uploaded `hello.txt`, `solution/`,
and `tests/required-artifacts.txt`. Full transcript: `captures/exec/demo.log`.

## Verdict

**Skip because the lab's unit of work is a versioned task directory plus an
experiment spec, not an ad-hoc compiled prompt.** Brief 05's proposer agents
must submit registered tasks; exec would bypass the registry, canary pin
(07), and migration (11). Keep the demo as a reference; do not wire exec into
the executor.
