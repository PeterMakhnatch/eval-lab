# HackBench: frozen-detector evaluation

This directory admits the pinned `facebook/hackbench` test release to DetBench.
It contains 169 positive records and 0 negative records.
All records have prior CM-11/CM-15 detector exposure.
This is a new DetBench adapter, not a new detector-unread source.

The requested evaluation measures frozen detector recall and coverage without tuning.
It cannot establish run-level AUROC, FPR, or superiority over length.
Those measures need labelled negative runs, which the publisher does not release.
Results must retain these limits and separate natural from synthetic records.

## Files

- `DATASET.md`: source construction, label provenance, rights, exposure, and conversion details.
- `manifest.json`: pinned revision, file sizes, SHA-256 digests, counts, and source integrity warnings.
- `label-map.json`: positive mapping, exclusion/failure rules, and prior exposure policy.
- `intake.py`: pinned source download and offline integrity verification.
- `adapter.py`: faithful, label-free DetBench conversion through the existing parser.
- `PREREG.md`: exact sample policy, frozen detector identities, thresholds, quota gates, and statistics.
- `evaluate.py`: preparation and parent-released evaluation; raw artifacts remain outside Git.
- `RESULT.md`: measured coverage, matched-row comparison, worked examples, and execution limits.
- `aggregate-results.json`: all individual rules, source strata, intervals, routes, versions, and quota readings.
- `sample-manifest.json` / `scored-sample.json`: exact selected and completed run identities, without transcripts.

Use the intake and conversion commands in `DATASET.md`.
Commit and push the adapter, map, and pre-registration before any detector reads the converted runs.
Do not change labels, prompts, thresholds, or rules after that barrier.

The single registered run completes 17 J2b records before the quota command fails.
J3 does not start. Do not interpret absent predictions as measured model failure.
`PREREG.md` records a later execution-control correction separately from the evaluated source identity.
The original freeze remains immutable; no scoring rerun follows the correction.

## Rights and data handling

The dataset uses CC-BY-NC-4.0 with additional no-training and no-rehosting conditions.
Use it only for non-commercial evaluation.
Keep raw transcripts, converted views, prompts, and judge replies outside Git.
The requested report may include only short worked-example passages, not raw transcripts.
No paid API, training, or vendor-store ingestion forms part of this evaluation.
