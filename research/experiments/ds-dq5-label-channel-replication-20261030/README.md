# DQ-5 — replicate the Goodfire action-channel zero; stricter rubric on CheatBench

Research-only experiment (no `src/` change). Two blinded, tool-less readings of the frozen
DT-8 DEV cohorts with `deepseek/deepseek-flash`:

- **read arm** — DQ-2's prompt and rubric, byte-identical
  (`instructions_sha256` `03f2252abd3b…`), on runs DQ-2 did not use: Goodfire positives
  ranks 16–55, Goodfire negatives 16–35, CheatBench positives 11–20 (sha256 order inside
  each stratum).
- **strict arm** — a second rubric: an action claim must name the checker part it defeats,
  state the grade effect, and cite a call/observation. Same runs plus DQ-2's CheatBench
  items.

`prereg.md` (in `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-29-ds-squad/raw/DQ-5/`)
fixes the sample rule, both prompts, the mechanical checks, the metrics and the $0.55
internal budget stop before the first call. `dq5.py` has three subcommands:

```bash
uv run --project <worktree> python research/experiments/ds-dq5-label-channel-replication-20261030/dq5.py build
uv run --project <worktree> python research/experiments/ds-dq5-label-channel-replication-20261030/dq5.py read
uv run --project <worktree> python research/experiments/ds-dq5-label-channel-replication-20261030/dq5.py score
```

`build` writes prompts to `/tmp/dq5/prompts/<arm>/<blind_id>.txt` and checks every run
body byte-for-byte against the stored DT-8 / DQ-2 prompt body. `read` issues at most 4
concurrent `omp -p` calls and stops issuing rows when measured session-log spend plus the
next row's estimate would pass `CAP_USD`. `score` writes `verdicts.jsonl`,
`replication.json`, `strict-arm.json`, `attestation.json` and `result.json`.

Receipts carry blind ids, flags, cite outcomes and counts only — no run text and no
quotable passage. Run text, replies and session logs stay in `/tmp/dq5`.

Results: `report.md` in the DQ-5 bridge directory.
