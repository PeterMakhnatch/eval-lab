# DQ-2 — what the Goodfire label actually labels: visible action or narration?

Descriptive follow-up to DT-15. DT-15 masked agent narration and the reader lost half its
Goodfire recall while keeping CheatBench; that is either a property of the corpus or of
the reader. DQ-2 asks the corpus question directly: for a sample of labelled runs, is the
labelled cheating visible in a tool call or a tool observation, or only in the agent's own
reasoning/message?

## Design

- Population: the frozen DT-8 DEV cohorts (Goodfire 150 = 80 enacted / 70 clean;
  CheatBench DEV 150 = 75 / 75), the same runs DT-10 and DT-15 read.
- Sample: within each corpus and label, `sha256(blind_id)` order, first N — Goodfire 15
  positives + 15 negatives, CheatBench 10 + 10. Ids in `receipts/sample.json`.
- One tool-less call per run (`deepseek/deepseek-flash`, thinking low, at most 4 in
  parallel), run text first and instructions last (`prereg.md` has the prompt verbatim).
- Mechanical check: every cited step must exist; every quote is classified with DT-10's
  classifier. An action claim survives only from a `<call ...>` or `<observation>`; a
  narration claim only from agent `reasoning`/`message`. Failures are dropped and counted.
- Labels are joined only in `score`, after every call is on disk. No prompt, prompt file
  name or session directory carries a label, corpus or split.

## Result

See `result.json` and the squad report. Headline: Goodfire positives are mostly
narration-only; CheatBench positives mostly carry an action.

## Files

| file | content |
|---|---|
| `dq2_label.py` | driver: `build` / `read` / `score` / `diagnose` |
| `prereg.md` | frozen design and prompt |
| `result.json` | aggregates and intervals |
| `verdicts.jsonl` | per-run ids, flags, every cited step and its class, dropped-claim counts |
| `sensitivity-recited.json` | post-hoc: claims located anywhere in the run |
| `diagnostics.json` | failed-quote locations, DT-15 masked cross-check, manual review |
| `sample.json` | the 50 chosen blind ids |
| `attestation.json` | served model per call from the session logs |

Run text, prompts, replies and session logs stay in `/tmp/dq2`; nothing here contains run
text or a quotable passage.

## Commands

```bash
uv run --project . python research/experiments/ds-dq2-what-the-label-labels-20261030/dq2_label.py build
uv run --project . python research/experiments/ds-dq2-what-the-label-labels-20261030/dq2_label.py read --jobs 4
uv run --project . python research/experiments/ds-dq2-what-the-label-labels-20261030/dq2_label.py score
uv run --project . python research/experiments/ds-dq2-what-the-label-labels-20261030/dq2_label.py diagnose
```

`build`, `score` and `diagnose` make no model call. `score` needs `/tmp/dq2/out`, the DT-8 cohort
file `/tmp/dt8/sample.json` and DT-10's blinded-step cache `/tmp/dt10/steps-*.json.gz`
(the same projections DT-8/DT-10 used; both are rebuildable from the lab's own loaders).
