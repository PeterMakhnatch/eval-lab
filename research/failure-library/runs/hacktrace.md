# hacktrace run collection

- Source: `https://huggingface.co/datasets/JH012/HackTrace-Data`.
- Pinned source revision: `291cf50ffc3cb4d2af42ade5e0cd728cb0037312`.
- Normalized rows: **20000**; JSONL SHA256 `ac7bbb68014e999b797f6d46fb7adb1864d7a7c038c3ff9b14fef33aedeae060`.
- Raw-source inventory: `54778e886c3fea81f5db557975b235302f353ead5643eb293bc7584e4a6f6e90` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/hacktrace.json`).
- Splits: `{"DEV": 13165, "cross_test": 1878, "heldout": 4957}`.
- Label counts (labels, not distinct runs): `{"construction": 12159, "executed": 26299, "judge": 61862}`.
- Outcome counts: `{"fail": 13701, "pass": 6299}`.
- Identity-linked runs: 0; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest hacktrace` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify hacktrace` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 20000, "by_signal": {"graded": 0, "monitored": 0, "tested": 1}, "mentions": 1, "sidecar_sha256": "7864b9439ec640325a693d28290a28cd7eacadf6b989e28c8b7d176d47c804ca"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{
  "population_rows": 173561,
  "sample_limit": 20000,
  "sampling": "lowest SHA256(native id), tie-break native id; source train/test retained"
}
```
