# termgrade run collection

- Source: `https://huggingface.co/datasets/ai-and/termgrade-trajectories`.
- Pinned source revision: `7d584d63dd598d7e76d444ca23e1527fb5ebd6d9`.
- Normalized rows: **36144**; JSONL SHA256 `a9fb7c9e1d63e4119c1fdc07480b5442d8ad69f22b6121f0cdc61d7121cdcfd0`.
- Raw-source inventory: `f02c31adc9899e8c5f814ddab9d7ba6a8862c9813120582be97c6466251eba5a` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/termgrade.json`).
- Splits: `{"DEV": 28800, "heldout": 7344}`.
- Label counts (labels, not distinct runs): `{"construction": 70, "executed": 620101}`.
- Outcome counts: `{"error": 68, "fail": 14166, "pass": 21910}`.
- Identity-linked runs: 0; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest termgrade` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify termgrade` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 36143, "by_signal": {"graded": 7487, "monitored": 150, "tested": 289}, "mentions": 7720, "sidecar_sha256": "69fad4d8bc39c3d49f5ceaec22ab427250e35990002a3ea61184bbb08355f246"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{
  "per_assertion_reports": 36074,
  "trials_in_scalar_table": 36144
}
```
