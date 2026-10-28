# terminal-wrench run collection

- Source: `https://huggingface.co/datasets/few-sh/terminal-wrench-trajectories`.
- Pinned source revision: `622449769ec6ddf33f770794489b8cd21712a0ad`.
- Normalized rows: **8641**; JSONL SHA256 `088ce0f0f4cf0f737ca8751f8c191fb2f72a0e6ef8d8391376e32beca37fbcdf`.
- Raw-source inventory: `0661b9feafeb58619d6d7ebb472b4515b95ebea03d354085c821b725885ec7b2` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/terminal-wrench.json`).
- Splits: `{"DEV": 3290, "cross_test": 4617, "heldout": 734}`.
- Label counts (labels, not distinct runs): `{"construction": 2352, "executed": 8641, "judge": 19788}`.
- Outcome counts: `{"fail": 1441, "pass": 7200}`.
- Identity-linked runs: 72; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key or linked finding forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. ImpossibleBench shares the task-side rule instead: full SHA256('livecodebench/' + base_id), or 'swe-bench/' + base_id, modulo 5 equals 0 for heldout, otherwise DEV. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.
Rebuild with `evallab failures runs ingest terminal-wrench` after populating the pinned raw cache (`--download` fetches public sources, `--input PATH` archives an existing cache). Re-run ingest to refresh finding links; `--findings-root DIR` can read another already-existing finding collection directory before it merges. Check `evallab failures runs verify terminal-wrench` and `evallab failures stats --json`.


RS mappings are source-label crosswalks, not fresh replay confirmations. Wrench's stored judgments remain judge labels; its verifier rewards alone are executed labels. Baseline and no_reward are not clean certificates. OLMo and ablation oracle flags remain executed; Goodfire and HackTrace judge decisions remain judge even when an executed outcome is also present. Human CheatBench labels remain human, and all CheatBench rows/conditions are cross_test.

## Eval-awareness first cut

Agent-prose regex only: `{"available": 72, "by_signal": {"graded": 13, "monitored": 0, "tested": 2}, "mentions": 15, "sidecar_sha256": "d7ab0ce576857989a6b368b909561fdd83b77788ec247613ceff7512ba02d51e"}`. Missing transcripts are counted as unavailable, not as unaware. Instructions and tool observations are excluded where the source has role metadata. The signals mention evaluation settings, monitors, or graders; they do not establish intent, knowledge, avoidance, or a causal monitoring effect.

## Source-specific details

```json
{
  "alternate_views_excluded": [
    "sanitized_hack",
    "stripped_hack"
  ],
  "original_runs_only": true
}
```

## RS mapping

| Native category | RS |
|---|---|
| algorithmic-simplification | RS01 |
| binary-hijacking | RS02 |
| constraint-loophole | RS01 |
| deceptive-rationalization | RS04 |
| hollow-implementation | RS01 |
| keyword-gaming | RS01 |
| metric-spoofing | RS01 |
| mutable-input-tampering | RS02 |
| output-spoofing | RS01 |
| security-downgrading | RS01 |
| structural-extraction | RS03 |

Only the 2,352 baseline + 3,632 hack + 2,657 non_hack original run lineages are ingested; sanitized and stripped views are excluded. Native transcripts not already cached keep pinned HF references, with regex coverage reported separately.
