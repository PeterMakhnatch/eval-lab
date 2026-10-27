# terminal-wrench run collection

- Source: `https://huggingface.co/datasets/few-sh/terminal-wrench-trajectories`.
- Pinned source revision: `622449769ec6ddf33f770794489b8cd21712a0ad`.
- Normalized rows: **8641**; JSONL SHA256 `11c7b7db87e0632e8b014eb55508f8150c88ff2a40a3366cb70175d2d45abc00`.
- Raw-source inventory: `3422d8768e825a5619497849b86cec7471395d071a4a7dd2a16ffcbe8a9ea8ba` (`$EVALLAB_FAILURE_LIBRARY_STATE/provenance/terminal-wrench.json`).
- Splits: `{"DEV": 3290, "cross_test": 4617, "heldout": 734}`.
- Label counts (labels, not distinct runs): `{"construction": 2352, "executed": 8641, "judge": 19788}`.
- Outcome counts: `{"fail": 1441, "pass": 7200}`.
- Identity-linked runs: 0; links across revisions are investigation leads, not defect confirmations.

## Admission and provenance

Only DEV can feed knowledge. Existing answer-key identities are matched against assignments.csv; any matching non-DEV key forces cross_test, even if another key calls the same identity DEV. Other new task families use SHA256(source_id + US + family), first 64 bits modulo 10: 0–7 DEV, 8–9 heldout. All variants/models of a family share its group. Explicit source test/heldout partitions remain excluded.

Transcript references locate immutable native files: #row=N is zero-based parquet, #line=N is one-based JSONL (including gzip), and &field= identifies the native field. No transcript bodies are copied into RunRecords or Git. Cached raw bytes are archived under the pinned revision; manifests count source rows, not independent scientific replications.

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
