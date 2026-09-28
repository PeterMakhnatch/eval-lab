---
status: living
audience:
  - analyst
  - operator
---

# Judging whether a task is good

A task is useful for evaluation or RL only if its reward means what it says: a pass is a real solve, a fail is a
real failure, and the task sits where the model sometimes passes and sometimes fails. Published datasets rarely
prove this. The FineEnvs MiMo Harbor ports, for example, ship no reference solution, so nobody can show a task is
solvable without running a model.

This page is the order of checks, cheapest first. Each stage only runs on tasks that survived the earlier ones.
The commands and tables are described in `docs/mimo-task-catalog.md`.

## Stage 0: read the files (free, no Docker)

`evallab tasks catalog build` records one row per task version in `task_versions` and one row per failed rule in
`task_findings` (`src/evallab/task_lint.py`).

| Question | Finding rule | Why it matters |
|---|---|---|
| Are these the bytes we think? | `mimo-manifest-digest-mismatch` | A silent upstream change invalidates every earlier result |
| Can anyone solve it? | `mimo-no-oracle` | Without an oracle, "no model passes" cannot separate hard from broken |
| Can the agent see the answer? | `mimo-answer-leak`, `mimo-git-history-readable` | A pass may be copying, not solving |
| Can the agent change the grader? | `mimo-verifier-not-isolated`, `mimo-terminal-hook-planting`, `mimo-testmain-plantable`, `mimo-conftest-plantable` | A root agent in the grading container can forge a pass |
| Can the agent fetch the fix? | `mimo-network-public` | Public network plus root means no network guarantee |
| Does grading cost money or depend on a third party? | `mimo-paid-judge`, `mimo-verify-network-dep` | Judges add cost and nondeterminism; grading downloads can fail |
| Will another runner set the task up? | `mimo-setup-healthcheck-only` | Runners that skip Harbor's healthcheck (e.g. Tinker's `harbor_rl` recipe) never run setup, so every attempt is ungradable |
| Do siblings straddle splits? | `split_group`, `split-group-unresolved` | A train/held-out split by task id leaks near-identical tasks |

## Stage 1: controls (free, local Docker)

- `nop` agent: reward must be 0 and the verifier must finish. A pass is a free reward; a verifier error means
  setup or grading is broken.
- Oracle: only where a solution exists (none in MiMo).

## Stage 2: rollouts (model cost, needs approval)

Run k attempts per task (4 to 8) with one pinned agent and model. `v_task_outcomes` gives the verdict per task
version and agent/model, with the Wilson 95% interval on the pass rate:

| Verdict | Meaning | Use |
|---|---|---|
| `learnable` | some attempts pass, some fail | RL signal; keep |
| `always_pass` | every scored attempt passes | no RL gradient; fine as a regression check |
| `always_fail` | every scored attempt fails | too hard or broken; audit before keeping |
| `infra_only` | no attempt produced a reward | infrastructure or setup problem, not a model result |
| `untested` | no trials | run it |

Errors are not zeros. A missing reward (setup failed, judge down, timeout in grading) must never be counted as a
fail, and must never become reward 0 in a training batch. Control agents (`nop`, oracle) get their own rows and
never count toward a model's verdict.

## Stage 3: audit the verdicts

- Flaky grading: rerun the verifier on the same final state with
  `--verifier evallab.harbor_repeat_verifier:RepeatVerifier --verifier-kwarg repeat_n=3`
  (`docs/task-stability.md`). A verdict that changes between reruns is noise; the MiMo report (§4.2.1) used 8.
- False passes: `src/evallab/mimo_exploit.py` marks a trial `suspected` when its trace reads hidden files, edits
  tests, `/etc/hosts` or reward files, or fetches the fix. A reviewer confirms with `confirm_exploit`. The hack
  probe (`research/experiments/mimo-hack-probe/probe.md`) deliberately asks a model to cheat, to find these holes
  before training does.
- False fails: for `always_fail` tasks, check whether the instruction and the hidden tests agree.

`v_task_audit` combines these: `train_eligible` requires `learnable`, stability evidence that says `stable`, and
no confirmed or unreviewed suspected exploit. `evallab tasks catalog export-eligible` also drops held-out tasks and
writes the list with its sha256, plus the digests of the tables it was built from.

## Stage 4: behavior

Turns, tokens, repeated tool calls ((N − U) / N over identical calls in a turn), and error classes, from
`evallab report run`. The independent model capture (`docs/model-capture.md`) shows whether the harness trace is
complete before any of these numbers are trusted. On the Terminus-2 lane it is not: summarization and handoff
calls never appear as trajectory steps.

## Changing a task

A fixed or rewritten task is a new version, never an edit in place. See `docs/task-variants.md`: the original
stays as the control, the variant records its parent, transform and changed files, and runs join to the exact
version they used.
