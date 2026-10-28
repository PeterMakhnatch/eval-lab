# metr-malt run collection

- Source: `https://huggingface.co/datasets/metr-evals/malt-public`.
- Pinned source revision: `ktolnos/rh-bench@1045a7336432c40182924bbd3698af292ea24acb`.
- Normalized rows: **572**; JSONL SHA256 `0e8f1d7a7c0faf1659a67ecffa88095e754dc282965dc020a8cb32f579972558`.
- Raw-source inventory: `9bfbdb482e93c6e900903604a61dc16f8775721eb9daf7de3e9b63cf87e60fbe` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/metr-malt.json`).
- Splits: `{"heldout": 572}`.
- Label counts (labels, not distinct runs): `{"human": 1144}`.
- Outcome counts: `{"unknown": 572}`.
- Identity-linked runs: 0; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest metr-malt` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify metr-malt` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 572, "by_signal": {"graded": 27, "monitored": 63, "tested": 33}, "mentions": 95, "sidecar_sha256": "ff089c3dbdbf30e448ede23ecb48200fc72110fa74a27b8c1eb9f53d5b7da0c6"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{
  "full_source_access": "HF_TOKEN authenticated; HTTP 403 GatedRepo at intake",
  "license": "CC-BY-SA-4.0 redistribution; original MALT MIT; benchmarking only",
  "missing": "task ids, full original messages/tool events and task revision",
  "redistribution_repo": "ktolnos/rh-bench",
  "redistribution_revision": "1045a7336432c40182924bbd3698af292ea24acb",
  "split_exception": "all heldout: redistribution omits task-family identity and is benchmark-only"
}
```

## Full-source access

The authenticated HF_TOKEN request returned HTTP 403 GatedRepo. Peter must request/accept access at https://huggingface.co/datasets/metr-evals/malt-public before the full 7,179-row default config can be downloaded. The local access-receipt.json records this attempt without credentials. The 572 redistributed rows are retained as text-only, heldout benchmarking data under the redistribution's CC-BY-SA-4.0 terms, not promoted to full native MALT runs.
