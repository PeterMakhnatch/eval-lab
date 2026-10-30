# DT-16 — do the render caps drive the reader?

Paired re-read of 60 DT-8 runs (30 Goodfire, 30 CheatBench DEV; 15 positive / 15
negative each) under the renderer's default caps and under wide caps. Driver and
summaries only; run text, prompts and model logs stay in `/tmp/dt16`.

- `dt16_caps.py` — `sample`, `build`, `read`, `score`.
- `receipts/sample.json` — chosen blind ids, realized cells, corpus cuts.
- `receipts/render-proof.json` — per-run markers and characters under both renderings,
  CheatBench unmarked cuts, and the DT-8 prompt join check.
- `receipts/readings-wide.jsonl` — wide verdicts (ids and verdicts only).
- `receipts/result.json` — paired McNemar, Wilson recall/FAR, narration mix, cost.
- `receipts/attestation.json` — served model per call.

Caps: original = `run_atlas` action 4,000 / observation 2,500 / context 12,000 and
`cheatbench` message 8,000 / observation 50,000; wide = 12,000 / 12,000 / 24,000 and
32,000 / 100,000. Constants are set in memory; `src/` is untouched.

The CheatBench wide rendering re-converts the pinned raw trace parquet
(`~/Developer/eval-lab-results/2026-10-19-trace-keys-cheatbench.parquet`,
sha256 `b4d2a149…`, the pinned `full.parquet`) because the published
`agent/trajectory.json` that DT-8 read already carries the importer's 8,000/50,000 caps.
