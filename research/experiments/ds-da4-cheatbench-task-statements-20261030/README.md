# DA-4 — CheatBench task statements from the dataset's own fields

DT-9 found that only 19 of 54 CheatBench alerts carried a task statement in the
run text, and that `corpus_tasks` / `agent_traces` hold no CheatBench rows, so a
task-conditioned monitor could not be built on the materialised projection. This
item recovers the statement from the source row instead — no answer-key file, no
model call.

## Result

78 of 150 DEV items (52.0% [44.1–59.8]) get a statement, from three fields inside
the pinned CheatBench row:

| source | DEV | DT-8 alerts (54) |
|---|---|---|
| `files["agent/command-0/command.txt"]`, the harness's quoted task payload | 44 | 10 |
| `files["agent/episode-N/prompt.txt"]` (Pilot episode prompt) | 7 | 1 |
| first `user` event ≥40 chars, not a `$NN` elision token | 27 | 9 |
| **total** | **78** | **20** |

By benchmark: swe-smith 23/23, terminal-bench-2 55/84, mle-bench 0/22,
bountybench 0/21. 3 of DT-8's 12 flagged negatives now have a statement. 0
statements carry label-bearing wording.

The materialised projection under
`eval-lab-results/2026-10-19/external-traces/<root>/{full,actions}/<trial>/agent/trajectory.json`
contains no user or context step at all, which is why the statement is missing
from the run text a monitor reads. A `$NN` token in an event `msg` is the
normaliser's elision placeholder; nothing in the row resolves it (0 of 14 rows
with such a token has a values table).

The remaining 72 rows carry only a task *identifier* (slug or path), never an
instruction; joining that identifier to the source benchmark's own task
definition is the only remaining route and is not done here.

## Usage

```bash
curl -sL -o /tmp/da4/full.parquet "https://huggingface.co/datasets/steinad/CheatBench/resolve/461360b34e93644955563dcc9243c6cc1a53bd0f/data/processed/parquet/full.parquet"
uv run --project . python research/experiments/ds-da4-cheatbench-task-statements-20261030/recover_tasks.py \
  --parquet /tmp/da4/full.parquet --out /tmp/da4
```

The script verifies the pinned sha256 before reading, writes
`/tmp/da4/tasks/<blind_id>.txt` (redacted with the benchmark/vendor pattern) and
`/tmp/da4/recovery.json` (per blind id: recovered, source, length, sha256, `$NN`
census; no text). `coverage.json` next to this README is the summary block of
that receipt with the per-run records removed.

Labels are read through `CorpusRegistry` (`expected_verdict`, DEV) and joined only
when counting; no label is written to a statement file.
