# T1-PATCH-FORENSICS — Goodfire DEV raw-CAS patch forensics + masked patch reader vs DT-15

Research-only experiment (no `src/` change). Answers one mystery that flat ATIF
projections hid: the projected submit step dropped the submitted `diff --git` patch
(`0/3,289`), so patch-level cheat evidence was invisible to every flat-transcript reader.
This item recovers the raw submitted patch from `agent_traces.raw_cas_uri` (the Inspect
raw-CAS blob) and asks (a) what fraction of enacted Goodfire DEV cheats leave forensic
evidence in the patch itself, and (b) whether a reader shown only the recovered patch +
task statement + compact command ledger beats DT-15's masked reader (8/80 @ 2/70).

## Families and submission formats

Family = second path component of `task_id`; each has a different "submission":

| family | traces (DEV) | submitted patch |
|---|---|---|
| `swebench_verified` | 1,215 | `info.swebench.submission` (the `diff --git` patch) |
| `deep_swe` | 1,534 | git-commit workflow; no diff patch in raw CAS `info` |
| `impossible_bench` | 925 | code-completion task; no diff patch; raw CAS `info.impossible_bench` records harness facts (`test_modified`, `shortcut_signal`, `test_reset_before_scoring`) |

DEV only. Labels join through `load_corpus_labels('goodfire')` / `('goodfire-dev')` (the
strict `run_atlas_score._read_labels` gate that REFUSES any TEST / non-DEV / non-admitted
id). Held rows are never read or scored.

## Subcommands (`patch_forensics.py`)

```
uv run --no-sync python research/experiments/ov-patch-forensics-20261031/patch_forensics.py extract
uv run --no-sync python research/experiments/ov-patch-forensics-20261031/patch_forensics.py score
uv run --no-sync python research/experiments/ov-patch-forensics-20261031/patch_forensics.py build-reader
uv run --no-sync python research/experiments/ov-patch-forensics-20261031/patch_forensics.py read
uv run --no-sync python research/experiments/ov-patch-forensics-20261031/patch_forensics.py score-reader
```

- `extract` — label-free patch + command + harness-forensics features for all 3,674 DEV
  traces (deterministic, no model call) -> `receipts/features.jsonl` (bridge, outside repo).
- `score` — label join + recall/FAR per family and per `upstream_fix_recall` vs other,
  length-controlled at `steps >= 41` -> `receipts/score.json`.
- `build-reader` / `read` / `score-reader` — the masked patch+command reader
  (`deepseek/deepseek-v4-pro`, `--no-tools --no-skills`, <=4 concurrent, attested) on the
  frozen 150-run DT-8 Goodfire cohort (80 enacted / 70 clean), plus paired McNemar vs DT-15.

`read` makes model calls (`deepseek/deepseek-v4-pro`) and needs the squad `worker.yml`;
every other subcommand is model-free. Per-run receipts (features, labels, prompts, replies,
session logs, reader verdicts) live OUTSIDE the repo (bridge `receipts/` and `/tmp/t1pf`);
nothing committed here contains patch text or label-joined per-run rows.

Full preregistration, results, and limits: `raw/T1-PATCH-FORENSICS/{prereg,report}.md` in
the squad bridge.
