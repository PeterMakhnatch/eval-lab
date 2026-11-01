# ov-deepswe-anatomy-20261031 — deep_swe blind-spot paired reader

Research-only experiment for `W2-DEEPSWE-BLIND-SPOT`: a paired two-arm reader on 100
length-matched `deep_swe` DEV runs (50 enacted / 50 clean) testing whether the lossless
`TraceAnatomy` view beats the old DT-15 truncated masked view.

- **Arm 1 (old DT-15 truncated masked view).** `run_atlas.render_step` (4,000-char call /
  2,500-char observation caps, source redaction) with agent narration (`reasoning_content`
  and `message`) replaced by `(tool calls only)`.
- **Arm 2 (lossless TraceAnatomy + backcheck final-claim).**
  `render_anatomy_card(include_narration=False, include_final_claim=True)` — private CoT is
  100% stripped while lossless recovered diffs, full-content protected heredoc/file writes,
  high-signal actions and claim-vs-reality checks are shown.

Both arms use the same fixed instruction block and the same reader
(`deepseek/deepseek-v4-pro`, `--no-tools --no-skills --thinking low`, at most 4 concurrent,
100% attested, fallback off).

## Files

- `ov_deepswe_anatomy.py` — the driver (`sample`, `build`, `read`, `score`, `subset`,
  `mechanism`). It imports `evallab.trace_anatomy` (PR #1689, now merged to `origin/main`).

## DEV-only boundaries

Labels are joined only through `load_corpus_labels('goodfire-dev')` (the strict
`run_atlas_score._read_labels` gate, which refuses TEST/non-DEV/non-admitted ids). The held
95 tasks (`split='held'`) and `research/answer-keys/` are never read or scored. Mechanism
labels (`source_label.categories`) are read at score time only, after every call is on disk.

## Repro

```bash
uv run --no-sync python research/experiments/ov-deepswe-anatomy-20261031/ov_deepswe_anatomy.py sample
uv run --no-sync python research/experiments/ov-deepswe-anatomy-20261031/ov_deepswe_anatomy.py build
uv run --no-sync python research/experiments/ov-deepswe-anatomy-20261031/ov_deepswe_anatomy.py read --arm arm2
uv run --no-sync python research/experiments/ov-deepswe-anatomy-20261031/ov_deepswe_anatomy.py subset --n 26
uv run --no-sync python research/experiments/ov-deepswe-anatomy-20261031/ov_deepswe_anatomy.py read --arm arm1 --only /tmp/w2deepswe/subset-arm1-ids.txt
uv run --no-sync python research/experiments/ov-deepswe-anatomy-20261031/ov_deepswe_anatomy.py mechanism
uv run --no-sync python research/experiments/ov-deepswe-anatomy-20261031/ov_deepswe_anatomy.py score
```

Preregistration and report live at
`research-context/bridge/2026-10-31-overnight/ds-pro/raw/W2-DEEPSWE-BLIND-SPOT/`.
