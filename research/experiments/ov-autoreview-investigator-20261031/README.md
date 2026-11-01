# W2-AUTOREVIEW-INVESTIGATOR: auto-review monitor on CheatBench DEV and Goodfire DEV

The first reader in this lab that is fed a built view instead of a transcript. The view is
`evallab.autoreview_monitor.build_compact_view` over `TraceAnatomy`: complete task statement,
mutation ledger (protected surfaces first), one line per tool call, reverted-edit ledger,
claim-vs-evidence backcheck, omission notice. Agent messages and `reasoning_content` are never
shown. Stage 1 scores 0-100 with a fixed verdict rubric; runs scoring 25..85 get stage 2 with
the raw tool call and output of the cited steps plus a path search. A split-half honest-quantile
threshold (ControlArena) is reported next to the fixed rubric.

Reader: `anthropic/claude-fable-5-1` via `omp -p --no-tools --no-skills` (attested per call from
the session log), at most 4 concurrent. Prereg, report and receipts live in
`research-context/bridge/2026-10-31-overnight/fable/raw/W2-AUTOREVIEW-INVESTIGATOR/`.

## Result

See `result.json` (copied from the bridge receipts). The gate verdicts and headline numbers
are in the bridge `report.md`.

## Usage

```bash
# statements for 78/150 CheatBench items (DA-4's recovery, no labels, pinned parquet sha256)
uv run --no-sync python research/experiments/ds-da4-cheatbench-task-statements-20261030/recover_tasks.py \
  --parquet /tmp/da4/full.parquet --out /tmp/da4
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python research/experiments/ov-autoreview-investigator-20261031/autoreview.py build
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python research/experiments/ov-autoreview-investigator-20261031/autoreview.py read --corpus cheatbench
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python research/experiments/ov-autoreview-investigator-20261031/autoreview.py read --corpus goodfire
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python research/experiments/ov-autoreview-investigator-20261031/autoreview.py score
```

Cohorts are DT-8's frozen rows (`ds-dt8-verified-reader-fullscale-20261029/dt8_reader.py`:
150 CheatBench DEV, 150 Goodfire DEV); labels are joined only in `build` (cohort rows) and
`score`. DT-15 masked readings (`ds-dt15-action-grounded-reader-20261030/readings-masked.jsonl`)
are the paired baseline. Goodfire mechanism categories come from `verified_receipt_json` on DEV
rows only.
