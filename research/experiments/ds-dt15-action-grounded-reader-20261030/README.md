# DT-15 — action-grounded reader (DT-8's reader with narration masked)

Same reader, prompt, schema and verification rule as DT-8, on the same 300 DEV runs, with
every agent narration block replaced by the renderer's `(tool calls only)` placeholder.
The only changed variable is the reader's input.

- `dt15_masked.py` — driver: `q0`, `mask`, `read`, `q2`, `score`, `attest`, `receipts`.
- `prereg.md` — frozen questions, sample rule, masking procedure, metrics and gates.
- `result.json` — rates, tertiles, length-matched difference, paired McNemar, cascade-v8
  union, citation classes, cost, gate verdicts.
- `q0-rescore.json` — post-hoc re-score of DT-8's stored readings on action/observation
  citations only.
- `readings-masked.jsonl` — per-run ids and verdicts only (no run text, no quotes).
- `attestation.json` — served model per call.
- `mask-report.json` — per-corpus masking counts and every run's identity-check status.

Reproduce (worktree with `evallab` installed, `EVALLAB_DISABLE_LOCAL_DOCKER=1`):

```bash
uv run --project . python research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py q0
uv run --project . python research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py mask
uv run --project . python research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py read --corpus goodfire
uv run --project . python research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py read --corpus cheatbench
uv run --project . python research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py q2
uv run --project . python research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py score
uv run --project . python research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py attest
```

`read` and `q2` make `omp` calls (`deepseek/deepseek-flash`, `--thinking low`,
`--no-tools`, four concurrent) and need the squad `worker.yml`; every other subcommand is
model-free. Run text, prompts, replies and session logs stay in `/tmp/dt15` and
`/tmp/dt10`; nothing committed here contains run text or quotes.

The rebuild and citation-classification helpers at the top of the driver are DT-10's
(`ds-dt10-explanation-swap-20261030/dt10_swap.py`, commit `a4abb3112`), copied because
that PR was not merged when this item ran.
