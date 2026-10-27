# goodfire-reward-hack-data run collection

- Source: `https://huggingface.co/datasets/goodfire/reward-hack-data`.
- Pinned source revision: `bd8fb473568b8ef1d12fc2325a5c8af939c15ad9`.
- Normalized rows: **6958**; JSONL SHA256 `a65b9c9b151f778d9e220ef065f560d05b98a967a0fbfaa23922ccf2d8330772`.
- Raw-source inventory: `cb1f7f188b112d0a1c3cf214e1ffdcd0890bd17db4b06a9e4a076b581b2f72e6` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/goodfire-reward-hack-data.json`).
- Splits: `{"DEV": 5364, "cross_test": 452, "heldout": 1142}`.
- Label counts (labels, not distinct runs): `{"executed": 6930, "judge": 17757}`.
- Outcome counts: `{"error": 28, "fail": 2628, "pass": 4302}`.
- Identity-linked runs: 935; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest goodfire-reward-hack-data` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify goodfire-reward-hack-data` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 6958, "by_signal": {"graded": 1898, "monitored": 228, "tested": 523}, "mentions": 2245, "sidecar_sha256": "6c00d0475480dde08af0450591efbb45c7ade8683b9d47f024e52cfe7554d1ea"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{
  "external_upload_allowed": false,
  "generic_monitor_streams_unimported_at_intake": 2999,
  "live_db_coding_streams_at_intake": 3959,
  "streams": {
    "deepswe-glm-5.2-fp8": 562,
    "deepswe-kimi-k3": 520,
    "deepswe-qwen3.8-2.4t-a95b-fp8": 452,
    "impossiblebench-glm-5.2-fp8": 307,
    "impossiblebench-kimi-k3": 309,
    "impossiblebench-qwen3.8-2.4t-a95b-fp8": 309,
    "shoppingbench-glm-5.2-20260828": 750,
    "shoppingbench-kimi-k3-20260828": 750,
    "swebench-verified-glm-5.2-nohack": 500,
    "swebench-verified-kimi-k3-nohack": 500,
    "swebench-verified-qwen3.8-2.4t-a95b-fp8-nohack": 500,
    "tau3bench-glm-5.2-fp8": 749,
    "tau3bench-kimi-k3": 750
  }
}
```
