# B6: CM-18 reader sensitivity (Sonnet 5.5 facets on a fixed CM-18 subsample)

Does the CM-18 facet result depend on the reader? The same production Run Atlas extractor and blinding re-read a fixed
subsample of the CM-18 sample with `anthropic/claude-sonnet-5-5`, scored at `suspicion >= 2` next to the stored
CM-18 Haiku 5.5 reads of exactly the same runs. Design: research-context `causal-prefix-monitor.md`, BURST slice B6.
Results: `RESULTS.md`.

- `tools/b6_cm18_sonnet.py`: `subsample`, `freeze`, `facets`, `score`, `spend`. Copied from
  `cm18-unseen-far-20261028/tools/cm18_unseen.py`; changed only: reader constant (Sonnet 5.5), private state directory
  (`~/.local/state/evallab-cm18-b6/`; the CM-18 state is read, never written), input-token cap 12M, concurrency capped
  at 4, `subsample` in place of `sample` (no adapter is opened; rows come from the frozen CM-18 `runs.pkl` after its
  sha256 and that of `sample.json` are checked against the committed CM-18 freeze), a strict `CorpusRegistry` cross-key
  guard over every run literal, a fallback-off gateway overlay (`PI_CONFIG_FILES`, read back as `false` before the first
  call), and a `score` that compares the two readers. The cascade and union are not rerun or rescored.
- Subsample: per set and stratum (positive, `negative_established`, `negative_other`), the first 15 runs of the CM-18
  sample in sha256(`order:` + key) order, the CM-18 facet processing order. `results/subsample.json` holds the key list
  (16-hex run-key hashes), its sha256, the counts and the cross-key result.
- `results/freeze.json`, `results/score.json` (aggregates only).
- Facet prose, per-run records and call receipts stay in `~/.local/state/evallab-cm18-b6/` (local, outside git, never
  uploaded). Receipts here hold counts and hashes only.
