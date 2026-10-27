# reward-hacking-ablation run collection

- Source: `https://huggingface.co/datasets/willjgriff/reward-hacking-ablation`.
- Pinned source revision: `dda898075de555d8bbf30b4598dbe5be878817a2`.
- Normalized rows: **182773**; JSONL SHA256 `44cbc1e7278b3bffae92689a4dca7c19258e98dc278f94f68ff43fd9ce6a9eb8`.
- Raw-source inventory: `1a23cd5c1daf3790cda2020d2caef5e527b6bd36944b2fc379bf6902c2910ab2` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/reward-hacking-ablation.json`).
- Splits: `{"DEV": 81681, "cross_test": 3058, "heldout": 98034}`.
- Label counts (labels, not distinct runs): `{"construction": 1162979, "executed": 536964}`.
- Outcome counts: `{"error": 210, "fail": 37038, "pass": 24733, "unknown": 120792}`.
- Identity-linked runs: 1738; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest reward-hacking-ablation` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify reward-hacking-ablation` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 182773, "by_signal": {"graded": 105499, "monitored": 96, "tested": 2744}, "mentions": 106034, "sidecar_sha256": "351cbb03f50c0adc9119704ad6abc78dd104a1350729bf6be0a96b0b9deb009b"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{
  "files_by_native_envelope": {
    "episodes.jsonl": 57,
    "records.jsonl.gz": 364,
    "trajectories.jsonl": 11,
    "trajectories.jsonl.gz": 120
  },
  "later_rhenvs_records": "All records.jsonl.gz exports included; plain oracle test flags stay executed, static/elicitation flags stay construction. Native family=heldout and MBPP split=test remain heldout. No scalar rewards are invented.",
  "license": "No dataset license declared by upstream; preserve locally",
  "row_identity": "source relative path plus native start line and trajectory/episode id; publisher-reused episode ids are preserved as distinct exported rows, not independent replication claims"
}
```
