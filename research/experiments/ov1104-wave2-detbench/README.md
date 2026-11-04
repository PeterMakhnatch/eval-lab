# Wave 2: DetBench methodology checks

## Questions and answers

X04 asks whether detector scores discriminate within the same task. The frozen snapshot contains 349 signal files across 25 sets. It has 113 signal-set rows where pooled AUROC exceeds within-task AUROC by more than 0.10. These rows are descriptive and not independent discoveries. Goodfire has no mixed-label task cluster, so its within-task AUROC cannot be estimated. Recorded reward is absent or does not yield both classes for every set, so the reward-only passing AUROC is unavailable. See `X04.md` for every signal and set, bootstrap intervals, denominators, and flags. `x04-aggregate.json.gz` also contains the natural and prompted strata and exposure metadata.

The post hoc `PASS_REPORT.md` separately analyzes Reflex-Sentinel's `pass_own` field. Its 87 passing rows include 43 cheats and 44 honest runs. The character-length AUROC is 0.673 (cluster bootstrap 95% CI 0.546–0.789). The field means the agent's own test file passes; it is not established as equivalent to `reward=1`.

X06 measures contamination among recorded `keep` decisions, excluding `review`. The replay keeps 30 cheats in 100 runs (30.0%, Wilson 95% CI 21.9%–39.6%) and 70 of 358 honest runs. At the DetBench matrix's frozen DEV operating points, the 4 selected combinations retain 27.8%–48.1% cheats in their kept populations. These are different filters with different missing-component semantics. See `X06.md` for the per-corpus tables, exact matrix parity checks, and the complete-case alternative. Countdown-Code's 1.2% is an experimental contamination dose, not a universal safety threshold.

The exploratory `cluster/REPORT.md` adds task-cluster bootstrap intervals for X06. The recorded replay keep fraction has a pooled 95% interval of 21.6%–39.8% over 190 clusters. The pooled bootstrap resamples clusters within each corpus. Some corpus intervals use only 4–6 clusters and are unstable or degenerate.

The post hoc `review/` sensitivity analysis finds 90 cheats in 338 review decisions (26.6%; cluster 95% CI 16.9%–36.2%). If every review were accepted, the hypothetical keep-plus-review set would contain 120 cheats in 438 runs (27.4%; 19.6%–35.5%). This union is not the recorded export.

The post hoc `prevalence/REPORT.md` reweights the fixed DEV operating points to assumed cheat base rates of 1%, 5%, and 20%. At a 5% base rate, the recorded replay keep decisions imply 2.37% contamination (cluster 95% CI 1.54%–3.77%). This calculation assumes the DEV class-conditional rates transfer; it is not a production estimate.

X05 could not faithfully reproduce the 17-rule scanner from the public paper and inspected code. `X05.md` documents the search and the missing implementation. No approximate detector was presented as the published scanner.

## Provenance and reproducibility

`PREREG.md` was hashed before scoring. The compressed X04 input manifest binds the runs, signal files, and individual view bytes. X06 receipts record the additional inputs and method. Raw run content is not included. Source data and original worker artifacts remain in `/private/tmp/ov1104/`; the source dataset may change or disappear. Published signals were already scored by other lanes, and many sets had prior detector exposure. These results are not blind confirmation.

The scripts are formatting-equivalent copies of the executed worker scripts. `x04.py` requires NumPy 2.5.4 and reads the frozen DetBench files. It writes its outputs beside the script, so run it from a scratch copy. Its full computation exceeds 5 minutes CPU and must use the program's heavy-job wrapper. `analyze.py` and `exact_matrix.py` reproduce the X06 tables from the local replay and DetBench files; run them from a scratch working directory. `exact_matrix.py` imports the specified DetBench scorer from the original lane worktree. All paths are local study paths, not portable dataset distributions.

The pre-registered X04 strict pair metric assigns ties zero; AUROC assigns ties half credit. The within-task AUROC averages mixed-label clusters equally. The strict pair fraction weights clusters by their pair counts. Missing clusters, scores, abstentions, component gaps, and reward fields are reported rather than treated as observed negatives, except where the exact matrix's own OR semantics treat missing components as non-firing. Confidence intervals do not correct dataset selection or prior exposure.
