# olmo-reward-hacking run collection

- Source: `https://huggingface.co/datasets/ai-safety-institute/reward-hacking-olmo3.1-32b-kl0.02-seed2-rollouts`.
- Pinned source revision: `fb89a90a4a4eeb287148dada7715dbdcc15f6cfe`.
- Normalized rows: **25792**; JSONL SHA256 `4a74ccc070994992f2dbf2ab281f79a4afabbe67d51300bd849bce184d1f06da`.
- Raw-source inventory: `3d3258d43eb4f7c1fa0a989913e3c1abd97b09a36be3758b81bf673b79c0cd4f` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/olmo-reward-hacking.json`).
- Splits: `{"DEV": 20336, "cross_test": 48, "heldout": 5408}`.
- Label counts (labels, not distinct runs): `{"executed": 146181}`.
- Outcome counts: `{"fail": 12405, "pass": 13358, "unknown": 29}`.
- Identity-linked runs: 0; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest olmo-reward-hacking` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify olmo-reward-hacking` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 25792, "by_signal": {"graded": 59, "monitored": 97, "tested": 24}, "mentions": 176, "sidecar_sha256": "d1acff821b3d52966a6d85f86075264c0b4f674d52ddcb6cc5251a2e1e00c6b7"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{}
```
