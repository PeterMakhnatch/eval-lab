# cheatbench run collection

- Source: `https://huggingface.co/datasets/steinad/CheatBench`.
- Pinned source revision: `461360b34e93644955563dcc9243c6cc1a53bd0f`.
- Normalized rows: **4472**; JSONL SHA256 `61a47003587056e6d2c7c302d14b6702217f2b372ebf09f07d7fb7bd96dbbdeb`.
- Raw-source inventory: `800fba7d97d497072272c7a7c06a6c9028b601ff35657fa9d72442fef4405415` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/cheatbench.json`).
- Splits: `{"cross_test": 4472}`.
- Label counts (labels, not distinct runs): `{"human": 9788}`.
- Outcome counts: `{"unknown": 4472}`.
- Identity-linked runs: 0; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest cheatbench` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify cheatbench` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 3376, "by_signal": {"graded": 104, "monitored": 156, "tested": 80}, "mentions": 333, "sidecar_sha256": "0f274e921679bc33a4615292f45f374b816777c6e474f1ca37dda444f33aa118"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{}
```
