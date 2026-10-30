# DT-3 — do the CheatBench importer caps hide cheating from a reader?

CheatBench is a calibration source for the detectors and the Atlas reader. The importer
(`src/evallab/cheatbench.py`) bounds every message at `MSG_CAP = 8_000` and every
observation at `OBS_CAP = 50_000` characters and appends `…[truncated]`. 54 of the 150
CheatBench DEV items carry that marker in the bytes the Atlas reader consumed. This
experiment asks whether those caps cost detections.

Design: the same 54 DEV runs, read twice by `deepseek/deepseek-flash` through the
production Atlas prompt, schema and blinding. The only changed variable is the importer
bound (on vs off, via the new opt-in `convert(..., caps=False)` argument). Labels are
joined only at scoring time, through `run_atlas_score.load_labels`. Full numbers, intervals,
the exact McNemar tests and the length control are in
`research-context/bridge/2026-10-29-ds-squad/raw/DT-3/report.md` (a study receipt, not
committed here); the preregistration predates every model call.

## Files

| File | What it is |
|---|---|
| `driver.py` | Cohort selection (marker scan + `sha256(item_id)` order), parquet row lookup by `source_id`, uncapped conversion, size census, and the two reading arms through `FacetExtractor` with a headless `omp -p` transport that attests the served model. |
| `score.py` | Label join, per-arm rates with Wilson intervals, paired exact McNemar, length tertiles, tokens and dollars. |
| `README.md` | This file. |

No transcripts and no per-run labels are committed. Reading files stay outside git.

## Reproduce

```bash
# Cohort + size census (no model call)
EVALLAB_ROOT=$PWD uv run --project . python research/experiments/ds-dt3-uncapped-20261029/driver.py \
  census --out /tmp/dt3/census.json

# Both arms (model calls; ~$1 with deepseek/deepseek-flash)
uv run --project . python research/experiments/ds-dt3-uncapped-20261029/driver.py \
  read --arm capped --census /tmp/dt3/census.json --out /tmp/dt3/readings-capped.jsonl
uv run --project . python research/experiments/ds-dt3-uncapped-20261029/driver.py \
  read --arm uncapped --census /tmp/dt3/census.json --out /tmp/dt3/readings-uncapped.jsonl

uv run --project . python research/experiments/ds-dt3-uncapped-20261029/score.py \
  --capped /tmp/dt3/readings-capped.jsonl --uncapped /tmp/dt3/readings-uncapped.jsonl \
  --census /tmp/dt3/census.json --out /tmp/dt3/result.json
```

The parquet is the pinned `steinad/CheatBench` full split
(`461360b34e93644955563dcc9243c6cc1a53bd0f`, sha256 `b4d2a1496e…160e63`), fetched with
`python -m evallab.cheatbench fetch`.

## Limits

DEV items only, no cross-test data. The reader is one model at one thinking level; the
`×[truncated]` bound is the only variable, so a difference is a property of this importer
and prompt, not of CheatBench in general.
