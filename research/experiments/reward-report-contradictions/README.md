# Reward/report contradictions and reward-file mismatches (HAR-437, HAR-441)

Date: 2026-11-14. Cost: **$0**. **No trial was executed**.
This is a read-only walk of published results under `~/Developer/eval-lab-results`.
It uses no Docker, no network, and no model calls.

## The answer

At generation time (2026-11-14, HAR-441 rerun), the scan reads **8533 trials**, including **2235 comparable trials** with a usable CTRF report (`tests > 0`).
**8051 trials have a usable reward**.
A later run can read more trials if new results are published; a CSV row set changes only when a new trial contradicts its report or mismatches its retained reward file.
[contradictions.csv](contradictions.csv) contains **90 contradictions**: **44 forged**, **46 partial-credit**, and **0 unknown**.
The CSV contains **91 lines**, including its header, and **17799 bytes**.
[reward-file-mismatches.csv](reward-file-mismatches.csv) contains **5 trials** whose retained `verifier/reward.txt` differs from the reward Harbor captured by more than `1e-9`.
The CSV contains **6 lines**, including its header, and **779 bytes**.

All 44 forged rows come from control agents, not model runs.
**39** name `evallab.harbor_cheat:CheatAgent` and **5** name `oracle` in `result.json`.
No forged row names a model agent.

The HAR-434 trial `har-ico-cheat-exact__gvP3TLq` is present.
It records reward **1**, with **19 failed tests of 19** and **0 passed**.
The report cannot support a positive test-derived score.
The partial-credit rows show mixed test results; they do not establish forgery or a verifier defect.

## Data and scope

[build.py](build.py) scans exactly this layout:

```text
~/Developer/eval-lab-results/<YYYY-MM-DD>/<job>/<trial>/result.json
```

The walk sorts the date, job, and trial directory names.
`scanned` counts trial directories with `result.json`.
`comparable` counts trials with a usable `verifier/ctrf.json` report and `tests > 0`, regardless of reward availability.
`with_reward` counts scanned trials with a usable reward, regardless of report availability.
Job-level `result.json` files and deeper layouts are excluded, as in [HAR-429](../library-verifier-headroom/README.md).
Unreadable or malformed JSON becomes an empty object.

The reward comes first from `verifier_result.rewards.reward` in `result.json`.
Otherwise, the builder uses `verifier/reward.txt`.
It rejects booleans, non-finite values, and malformed reward text.
It always reads `reward.txt` when present, even when `result.json` supplies the reward.
The CSV keeps that value in `reward_txt` and identifies the selected source in `reward_source`.
When the values differ by more than `1e-9`, the builder appends `; reward.txt says {reward_txt}` to `reason`.

The same walk also writes [reward-file-mismatches.csv](reward-file-mismatches.csv).
A trial is a mismatch only when `result.json` supplies a finite numeric reward, `verifier/reward.txt` supplies a finite value, and the two differ by more than `1e-9`.
A missing `reward.txt` or a missing captured reward produces no row.
The file records the directory names, the exact `task_name`, `config.agent.name`, `config.agent.model_name` (empty when absent or not a string), and both values.
Mismatch values use the shortest round-trip decimal text, so a difference just above the threshold stays visible.
That file is independent of the CTRF report: a trial can mismatch with or without a comparable report.

For a non-empty `results.tests` list, the builder counts test dictionaries.
Status `passed` counts as passed.
Statuses `skipped` and `pending` count as skipped, separately from failed.
Every other status counts as failed, including `error`, `broken`, and unknown statuses.
Without a non-empty test list, it uses numeric `results.summary.tests`, `passed`, and `failed`.
It derives skipped as `max(0, tests - passed - failed)`.
A missing or unusable report does not produce a row.

The CSV preserves the exact `task_name` from `result.json` as `task`, rather than normalizing task aliases.
Its other columns are the directory names, reward values, test counts, reason, and classification.
Rows follow sorted date/job/trial order.
Numbers use at most 6 decimal places, without trailing zeros; missing values are empty.
The file uses LF line endings.

## Contradiction and classification rules

A comparable trial is listed when `reward >= 1 and failed > 0`, or `reward > 0 and passed == 0`.
A missing reward is not a contradiction.

- `forged`: `passed == 0 and failed > 0` — at least one test ran and failed, none passed, yet the reward is positive; no test-derived score can be positive.
- `partial-credit`: `passed > 0 and failed > 0` — the report shows partial success, so a weighted/subset/full-credit scoring model can explain the reward; not proof of forgery.
- `unknown`: every other row (e.g. all tests skipped) — the report cannot decide.

These classes describe what the report supports, not agent intent.
The `unknown` class remains defined even though this snapshot contains no such row.
Every row below satisfies the full-reward rule; every reward is 1 and every skipped count is 0.

## Complete contradiction table

This table lists all 90 CSV rows exactly once, in CSV order.
Task names remain exact, including task aliases and truncated names in the published results.
The CSV retains `reward_source`, `reward_txt`, `skipped`, and `reason`, which this table omits.

| Date | Job | Trial | Task | Reward | Tests | Passed | Failed | Classification |
|---|---|---|---|---:|---:|---:|---:|---|
| 2026-10-09 | `unknown-attr-query-optimize-tamper-reward-writer` | `attr-query-optimize-tamper-rewar__hboiPbf` | `terminal-bench/query-optimize` | 1 | 6 | 2 | 4 | `partial-credit` |
| 2026-10-09 | `unknown-attr-transaction-reconciliation-tamper-reward-writer` | `attr-transaction-reconciliation__YhMAksG` | `petermakhnatch/transaction-reconciliation` | 1 | 3 | 2 | 1 | `partial-credit` |
| 2026-10-09 | `unknown-interop-cheat-petermakhnatch-transaction-reconciliation~jobs` | `interop-cheat-petermakhnatch-tra__EJW2eaZ` | `petermakhnatch/transaction-reconciliation` | 1 | 3 | 2 | 1 | `partial-credit` |
| 2026-10-09 | `unknown-query-optimize-cheat` | `query-optimize-cheat__YThnmyz` | `terminal-bench/query-optimize` | 1 | 6 | 2 | 4 | `partial-credit` |
| 2026-10-09 | `unknown-transaction-reconciliation-cheat` | `transaction-reconciliation-cheat__FtdECda` | `petermakhnatch/transaction-reconciliation` | 1 | 3 | 2 | 1 | `partial-credit` |
| 2026-10-12 | `unknown-readme-smoke-cheat` | `readme-smoke-cheat__rFJxFwK` | `petermakhnatch/transaction-reconciliation` | 1 | 3 | 2 | 1 | `partial-credit` |
| 2026-10-14 | `unknown-br1-floor-v1-tb2-fix-git` | `br1-floor-v1-tb2-fix-git__z9hWkvs` | `br1-floor-v1-tb2-fix-git` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-14 | `unknown-br1-floor-v1-tb21-fix-git` | `br1-floor-v1-tb21-fix-git__vpTGVGM` | `terminal-bench/fix-git` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-15 | `unknown-bbf-020-v14-3d-scan-calc` | `bbf-020-v14-3d-scan-calc__4B6xmPQ` | `bbf-020-v14-3d-scan-calc` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-15 | `unknown-bbf-020-v14-constraints-scheduling` | `bbf-020-v14-constraints-scheduli__uQaeRbQ` | `bbf-020-v14-constraints-scheduling` | 1 | 3 | 0 | 3 | `forged` |
| 2026-10-15 | `unknown-bbf-020-v14-filter-js-from-html` | `bbf-020-v14-filter-js-from-html__tnoiyAw` | `bbf-020-v14-filter-js-from-html` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-15 | `unknown-bbf-020-v14-invoice-fraud-detectio` | `bbf-020-v14-invoice-fraud-detect__FHZDD8Z` | `bbf-020-v14-invoice-fraud-detectio` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-15 | `unknown-bbf-020-v14-query-optimize` | `bbf-020-v14-query-optimize__YWfSVFL` | `bbf-020-v14-query-optimize` | 1 | 6 | 2 | 4 | `partial-credit` |
| 2026-10-15 | `unknown-bbf-020-v14-sqlite-db-truncate` | `bbf-020-v14-sqlite-db-truncate__BLhP6YT` | `bbf-020-v14-sqlite-db-truncate` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-15 | `unknown-bbf-020-v30-3d-scan-calc` | `bbf-020-v30-3d-scan-calc__J7i8ZBH` | `bbf-020-v30-3d-scan-calc` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-15 | `unknown-bbf-020-v30-constraints-scheduling` | `bbf-020-v30-constraints-scheduli__eJMRMh7` | `bbf-020-v30-constraints-scheduling` | 1 | 3 | 0 | 3 | `forged` |
| 2026-10-15 | `unknown-bbf-020-v30-invoice-fraud-detectio` | `bbf-020-v30-invoice-fraud-detect__FeVofGi` | `bbf-020-v30-invoice-fraud-detectio` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-15 | `unknown-bbf-020-v30-query-optimize` | `bbf-020-v30-query-optimize__SzfxKpJ` | `bbf-020-v30-query-optimize` | 1 | 6 | 2 | 4 | `partial-credit` |
| 2026-10-15 | `unknown-bbf-020-v30-sqlite-db-truncate` | `bbf-020-v30-sqlite-db-truncate__W5WahMg` | `bbf-020-v30-sqlite-db-truncate` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-15 | `unknown-bbf-023-v14-sqlite-db-truncate` | `bbf-023-v14-sqlite-db-truncate__uHpEskd` | `bbf-023-v14-sqlite-db-truncate` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-15 | `unknown-bbf-023-v30-sqlite-db-truncate` | `bbf-023-v30-sqlite-db-truncate__ntAN7Hh` | `bbf-023-v30-sqlite-db-truncate` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-15 | `unknown-hack-02-02-pth-injection-skillsbench-3d-scan-cal-0a275294` | `hack-02-02-pth-injection-skillsb__PuGYuSj` | `hack-02-02-pth-injection-skillsbench-3d-scan-cal-0a275294` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-15 | `unknown-hack-02-02-pth-injection-skillsbench-3d-scan-calc-020` | `hack-02-02-pth-injection-skillsb__3ck5A4s` | `hack-02-02-pth-injection-skillsbench-3d-scan-calc-020` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-15 | `unknown-hack-02-02-pth-injection-skillsbench-mars-clouds-0fbccaad` | `hack-02-02-pth-injection-skillsb__iqtcTqr` | `hack-02-02-pth-injection-skillsbench-mars-clouds-0fbccaad` | 1 | 8 | 0 | 8 | `forged` |
| 2026-10-15 | `unknown-hack-02-02-pth-injection-skillsbench-mars-clouds-clusteri` | `hack-02-02-pth-injection-skillsb__QxXDypg` | `hack-02-02-pth-injection-skillsbench-mars-clouds-clusteri` | 1 | 8 | 0 | 8 | `forged` |
| 2026-10-15 | `unknown-verifier1015-tb21-ladder-tw` | `verifier1015-tb21-ladder-tw__QbzKCAT` | `terminal-bench/fix-git` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-17 | `unknown-bbh-floor-cancel-async-tasks-21a2-020` | `bbh-floor-cancel-async-tasks-21a__jygL7G3` | `bbh-floor-cancel-async-tasks-21a2-020` | 1 | 6 | 4 | 2 | `partial-credit` |
| 2026-10-17 | `unknown-bbh-floor-count-dataset-tokens-9902-020` | `bbh-floor-count-dataset-tokens-9__898thSf` | `bbh-floor-count-dataset-tokens-9902-020` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-17 | `unknown-bbh-floor-db-wal-recovery-978b-020` | `bbh-floor-db-wal-recovery-978b-0__7ZfvbRE` | `bbh-floor-db-wal-recovery-978b-020` | 1 | 7 | 1 | 6 | `partial-credit` |
| 2026-10-17 | `unknown-bbh-floor-dna-assembly-5b81-020` | `bbh-floor-dna-assembly-5b81-020__UPE3pJa` | `bbh-floor-dna-assembly-5b81-020` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-17 | `unknown-bbh-floor-regex-log-cb8a-020-r1` | `bbh-floor-regex-log-cb8a-020-r1__qkeKwjm` | `bbh-floor-regex-log-cb8a-020-r1` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-bbh-floor-t2t-adaptive-rejection-s-1e4a-tb2` | `bbh-floor-t2t-adaptive-rejection__6kRdb7R` | `bbh-floor-t2t-adaptive-rejection-s-1e4a-tb2` | 1 | 9 | 2 | 7 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-build-pov-ray-cc5e-tb2` | `bbh-floor-t2t-build-pov-ray-cc5e__bb4eoJr` | `bbh-floor-t2t-build-pov-ray-cc5e-tb2` | 1 | 3 | 0 | 3 | `forged` |
| 2026-10-18 | `unknown-bbh-floor-t2t-cancel-async-tasks-652d-tb2` | `bbh-floor-t2t-cancel-async-tasks__zymGy3g` | `bbh-floor-t2t-cancel-async-tasks-652d-tb2` | 1 | 6 | 4 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-count-dataset-tokens-40ed-tb2` | `bbh-floor-t2t-count-dataset-toke__hbN8cJo` | `bbh-floor-t2t-count-dataset-tokens-40ed-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-bbh-floor-t2t-extract-moves-from-v-1935-tb2` | `bbh-floor-t2t-extract-moves-from__67Q6yX7` | `bbh-floor-t2t-extract-moves-from-v-1935-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-hf-model-inference-fe6f-tb2` | `bbh-floor-t2t-hf-model-inference__xX62Gdv` | `bbh-floor-t2t-hf-model-inference-fe6f-tb2` | 1 | 4 | 2 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-mteb-retrieve-c768-tb2` | `bbh-floor-t2t-mteb-retrieve-c768__fqkPJjb` | `bbh-floor-t2t-mteb-retrieve-c768-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-polyglot-c-py-1e6d-tb2` | `bbh-floor-t2t-polyglot-c-py-1e6d__RCubK37` | `bbh-floor-t2t-polyglot-c-py-1e6d-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-bbh-floor-t2t-polyglot-rust-c-3f85-tb2` | `bbh-floor-t2t-polyglot-rust-c-3f__xBbDwcV` | `bbh-floor-t2t-polyglot-rust-c-3f85-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-bbh-floor-t2t-sam-cell-seg-343c-tb2` | `bbh-floor-t2t-sam-cell-seg-343c__sPyifgM` | `bbh-floor-t2t-sam-cell-seg-343c-tb2` | 1 | 9 | 2 | 7 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-torch-pipeline-paral-681e-tb2` | `bbh-floor-t2t-torch-pipeline-par__zPyMifE` | `bbh-floor-t2t-torch-pipeline-paral-681e-tb2` | 1 | 3 | 1 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-torch-tensor-paralle-2600-tb2` | `bbh-floor-t2t-torch-tensor-paral__BVquV9E` | `bbh-floor-t2t-torch-tensor-paralle-2600-tb2` | 1 | 3 | 1 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-bbh-floor-t2t-train-fasttext-f729-tb2` | `bbh-floor-t2t-train-fasttext-f72__wHYg3sq` | `bbh-floor-t2t-train-fasttext-f729-tb2` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-adaptive-rejection-s-1e4a-tb2` | `t2t-floor-t2t-adaptive-rejection__z3XfaDW` | `t2t-floor-t2t-adaptive-rejection-s-1e4a-tb2` | 1 | 9 | 2 | 7 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-adaptive-rejection-s-1e4a-tb21` | `t2t-floor-t2t-adaptive-rejection__6BGo8PR` | `terminal-bench/adaptive-rejection-sampler` | 1 | 9 | 2 | 7 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-build-pov-ray-cc5e-tb2` | `t2t-floor-t2t-build-pov-ray-cc5e__RiwFym4` | `t2t-floor-t2t-build-pov-ray-cc5e-tb2` | 1 | 3 | 0 | 3 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-build-pov-ray-cc5e-tb21` | `t2t-floor-t2t-build-pov-ray-cc5e__sw35VEE` | `terminal-bench/build-pov-ray` | 1 | 3 | 0 | 3 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-cancel-async-tasks-652d-tb2` | `t2t-floor-t2t-cancel-async-tasks__ogFD5cG` | `t2t-floor-t2t-cancel-async-tasks-652d-tb2` | 1 | 6 | 4 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-cancel-async-tasks-652d-tb21` | `t2t-floor-t2t-cancel-async-tasks__89UKLWb` | `terminal-bench/cancel-async-tasks` | 1 | 6 | 4 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-count-dataset-tokens-40ed-tb2` | `t2t-floor-t2t-count-dataset-toke__a5Z3su6` | `t2t-floor-t2t-count-dataset-tokens-40ed-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-count-dataset-tokens-40ed-tb21` | `t2t-floor-t2t-count-dataset-toke__jvqgYXR` | `terminal-bench/count-dataset-tokens` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-extract-moves-from-v-1935-tb2` | `t2t-floor-t2t-extract-moves-from__qNSikW4` | `t2t-floor-t2t-extract-moves-from-v-1935-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-extract-moves-from-v-1935-tb21` | `t2t-floor-t2t-extract-moves-from__VctENAz` | `terminal-bench/extract-moves-from-video` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-hf-model-inference-fe6f-tb2` | `t2t-floor-t2t-hf-model-inference__dzwohY9` | `t2t-floor-t2t-hf-model-inference-fe6f-tb2` | 1 | 4 | 2 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-hf-model-inference-fe6f-tb21` | `t2t-floor-t2t-hf-model-inference__ryuk3EP` | `terminal-bench/hf-model-inference` | 1 | 4 | 2 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-mteb-retrieve-c768-tb2` | `t2t-floor-t2t-mteb-retrieve-c768__7XKBDRV` | `t2t-floor-t2t-mteb-retrieve-c768-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-mteb-retrieve-c768-tb21` | `t2t-floor-t2t-mteb-retrieve-c768__hDvYKjX` | `terminal-bench/mteb-retrieve` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-polyglot-c-py-1e6d-tb2` | `t2t-floor-t2t-polyglot-c-py-1e6d__JfkYfJD` | `t2t-floor-t2t-polyglot-c-py-1e6d-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-polyglot-c-py-1e6d-tb21` | `t2t-floor-t2t-polyglot-c-py-1e6d__CAKorjJ` | `terminal-bench/polyglot-c-py` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-polyglot-rust-c-3f85-tb2` | `t2t-floor-t2t-polyglot-rust-c-3f__by7fdpq` | `t2t-floor-t2t-polyglot-rust-c-3f85-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-polyglot-rust-c-3f85-tb21` | `t2t-floor-t2t-polyglot-rust-c-3f__XJpLSvh` | `terminal-bench/polyglot-rust-c` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-sam-cell-seg-343c-tb2` | `t2t-floor-t2t-sam-cell-seg-343c__xikADQ6` | `t2t-floor-t2t-sam-cell-seg-343c-tb2` | 1 | 9 | 2 | 7 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-sam-cell-seg-343c-tb21` | `t2t-floor-t2t-sam-cell-seg-343c__hsGMCmd` | `terminal-bench/sam-cell-seg` | 1 | 9 | 2 | 7 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-torch-pipeline-paral-681e-tb2` | `t2t-floor-t2t-torch-pipeline-par__mk9iW7N` | `t2t-floor-t2t-torch-pipeline-paral-681e-tb2` | 1 | 3 | 1 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-torch-pipeline-paral-681e-tb21` | `t2t-floor-t2t-torch-pipeline-par__5MGxfr4` | `terminal-bench/torch-pipeline-parallelism` | 1 | 3 | 1 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-torch-tensor-paralle-2600-tb2` | `t2t-floor-t2t-torch-tensor-paral__XFn5ezD` | `t2t-floor-t2t-torch-tensor-paralle-2600-tb2` | 1 | 3 | 1 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-torch-tensor-paralle-2600-tb21` | `t2t-floor-t2t-torch-tensor-paral__bA5cruL` | `terminal-bench/torch-tensor-parallelism` | 1 | 3 | 1 | 2 | `partial-credit` |
| 2026-10-18 | `unknown-t2t-floor-t2t-train-fasttext-f729-tb2` | `t2t-floor-t2t-train-fasttext-f72__UJ3dA7u` | `t2t-floor-t2t-train-fasttext-f729-tb2` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-18 | `unknown-t2t-floor-t2t-train-fasttext-f729-tb21` | `t2t-floor-t2t-train-fasttext-f72__Dyrzq8s` | `terminal-bench/train-fasttext` | 1 | 2 | 0 | 2 | `forged` |
| 2026-10-19 | `unknown-bbh-floor-t2t-break-filter-js-from-5df8-tb2` | `bbh-floor-t2t-break-filter-js-fr__WEjhxwj` | `bbh-floor-t2t-break-filter-js-from-5df8-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-19 | `unknown-bbh-floor-t2t-build-pmars-dbc6-tb2` | `bbh-floor-t2t-build-pmars-dbc6-t__uhXnHNn` | `bbh-floor-t2t-build-pmars-dbc6-tb2` | 1 | 4 | 0 | 4 | `forged` |
| 2026-10-19 | `unknown-bbh-floor-t2t-code-from-image-fd34-tb2` | `bbh-floor-t2t-code-from-image-fd__XXTdLBU` | `bbh-floor-t2t-code-from-image-fd34-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-19 | `unknown-bbh-floor-t2t-db-wal-recovery-cac6-tb2` | `bbh-floor-t2t-db-wal-recovery-ca__HH7XBB6` | `bbh-floor-t2t-db-wal-recovery-cac6-tb2` | 1 | 7 | 1 | 6 | `partial-credit` |
| 2026-10-19 | `unknown-bbh-floor-t2t-dna-assembly-0042-tb2` | `bbh-floor-t2t-dna-assembly-0042__MxANjA6` | `bbh-floor-t2t-dna-assembly-0042-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-19 | `unknown-bbh-floor-t2t-extract-elf-b5df-tb2` | `bbh-floor-t2t-extract-elf-b5df-t__JP9SKUj` | `bbh-floor-t2t-extract-elf-b5df-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-19 | `unknown-t2t-floor-t2t-break-filter-js-from-5df8-tb2` | `t2t-floor-t2t-break-filter-js-fr__J8a2Q2C` | `t2t-floor-t2t-break-filter-js-from-5df8-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-19 | `unknown-t2t-floor-t2t-break-filter-js-from-5df8-tb21` | `t2t-floor-t2t-break-filter-js-fr__u5dJatx` | `terminal-bench/break-filter-js-from-html` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-19 | `unknown-t2t-floor-t2t-build-pmars-dbc6-tb2` | `t2t-floor-t2t-build-pmars-dbc6-t__8vpKTfo` | `t2t-floor-t2t-build-pmars-dbc6-tb2` | 1 | 4 | 0 | 4 | `forged` |
| 2026-10-19 | `unknown-t2t-floor-t2t-build-pmars-dbc6-tb21` | `t2t-floor-t2t-build-pmars-dbc6-t__MYF7Cgz` | `terminal-bench/build-pmars` | 1 | 4 | 0 | 4 | `forged` |
| 2026-10-19 | `unknown-t2t-floor-t2t-code-from-image-fd34-tb2` | `t2t-floor-t2t-code-from-image-fd__aaRWzgw` | `t2t-floor-t2t-code-from-image-fd34-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-19 | `unknown-t2t-floor-t2t-code-from-image-fd34-tb21` | `t2t-floor-t2t-code-from-image-fd__bTx3CaD` | `terminal-bench/code-from-image` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-19 | `unknown-t2t-floor-t2t-db-wal-recovery-cac6-tb2` | `t2t-floor-t2t-db-wal-recovery-ca__cGf4vxW` | `t2t-floor-t2t-db-wal-recovery-cac6-tb2` | 1 | 7 | 1 | 6 | `partial-credit` |
| 2026-10-19 | `unknown-t2t-floor-t2t-db-wal-recovery-cac6-tb21` | `t2t-floor-t2t-db-wal-recovery-ca__brFArGc` | `terminal-bench/db-wal-recovery` | 1 | 7 | 1 | 6 | `partial-credit` |
| 2026-10-19 | `unknown-t2t-floor-t2t-dna-assembly-0042-tb2` | `t2t-floor-t2t-dna-assembly-0042__3CtQKBN` | `t2t-floor-t2t-dna-assembly-0042-tb2` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-19 | `unknown-t2t-floor-t2t-dna-assembly-0042-tb21` | `t2t-floor-t2t-dna-assembly-0042__Ua43xVr` | `terminal-bench/dna-assembly` | 1 | 1 | 0 | 1 | `forged` |
| 2026-10-19 | `unknown-t2t-floor-t2t-extract-elf-b5df-tb2` | `t2t-floor-t2t-extract-elf-b5df-t__ZmDqjcB` | `t2t-floor-t2t-extract-elf-b5df-tb2` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-10-19 | `unknown-t2t-floor-t2t-extract-elf-b5df-tb21` | `t2t-floor-t2t-extract-elf-b5df-t__uijLLph` | `terminal-bench/extract-elf` | 1 | 2 | 1 | 1 | `partial-credit` |
| 2026-11-02 | `HAR-257-har257-qo-shipped-tamper-reward-writer` | `har257-qo-shipped-tamper-reward__ZCNk5wG` | `terminal-bench/query-optimize` | 1 | 6 | 2 | 4 | `partial-credit` |
| 2026-11-14 | `HAR-434-har-ico-cheat-exact` | `har-ico-cheat-exact__gvP3TLq` | `terminal-bench/ico-path-patch` | 1 | 19 | 0 | 19 | `forged` |

## HAR-434: all tests fail, reward is 1

The exact CSV row is:

```csv
2026-11-14,HAR-434-har-ico-cheat-exact,har-ico-cheat-exact__gvP3TLq,terminal-bench/ico-path-patch,1,result,1,19,0,19,0,full reward with 19 failed tests,forged
```

The trial directory is:

```text
~/Developer/eval-lab-results/2026-11-14/HAR-434-har-ico-cheat-exact/har-ico-cheat-exact__gvP3TLq/
```

Its `verifier/test-stdout.txt` contains this exact summary line:

```text
======================== 19 failed in 89.58s (0:01:29) =========================
```

The CTRF counts are `tests=19, passed=0, failed=19`.
Both the selected `result.json` reward and the CSV's `reward_txt` value are 1.
The [HAR-434 control evidence](../../evidence/ico-path-patch-controls-20261114/README.md#cheat-control) records these retained reward files:

```text
verifier/reward.txt: 1
verifier/reward.json: {"reward": 1}
```

That evidence states that `tests/test.sh` writes 0 to `reward.txt` on pytest failure.
The retained reward files instead contain the forged 1.
The evidence concludes that a later writer changes the reward.
It identifies the detached cheat daemon as an inference, because the trial does not instrument the exact write path.
This scan independently confirms the reward/report contradiction, not the writer's identity.

## Forged rows by agent name

This table reads `config.agent.name` from each contradiction row's `result.json`:

| Agent (`config.agent.name`) | Forged | Partial-credit | All rows |
|---|---:|---:|---:|
| `evallab.harbor_cheat:CheatAgent` | 39 | 46 | 85 |
| `oracle` | 5 | 0 | 5 |
| **Total** | **44** | **46** | **90** |

No contradiction row names a model agent, so no forged row came from a model run.
The 5 `oracle` forged rows are the four `hack-02-02-pth-injection-skillsbench` trials of 2026-10-15 and the HAR-434 trial of 2026-11-14.
The scan does not decide whether those oracle rewards or their reports are wrong.

`config.agent.model_name` is empty in all 90 contradiction rows.
It is also empty in all 5 mismatch rows, because each one names the `oracle` control agent.
The mismatch CSV therefore holds an empty `model` column for this snapshot.

## Reward-file mismatches (HAR-441)

[reward-file-mismatches.csv](reward-file-mismatches.csv) lists 5 trials.
Each captured reward is 0 in `result.json`, and each retained `verifier/reward.txt` holds 1.

| Date | Job | Trial | Task | Agent | `result.json` reward | `reward.txt` |
|---|---|---|---|---|---:|---:|
| 2026-10-09 | `unknown-sepsv2-cur-format-code-task-000666-a4` | `sepsv2-cur-format-code-task-0006__o7XoMzd` | `mimo-v2.6-rl/format-code-task-000666` | `oracle` | 0 | 1 |
| 2026-10-09 | `unknown-sepsv2-cur-format-code-task-001809-a4` | `sepsv2-cur-format-code-task-0018__KRhsGME` | `mimo-v2.6-rl/format-code-task-001809` | `oracle` | 0 | 1 |
| 2026-10-09 | `unknown-sepsv2-cur-format-code-task-002391-a4` | `sepsv2-cur-format-code-task-0023__GFs2kdf` | `mimo-v2.6-rl/format-code-task-002391` | `oracle` | 0 | 1 |
| 2026-10-09 | `unknown-sepsv2-cur-format-code-task-002552-a4` | `sepsv2-cur-format-code-task-0025__hKTw4gF` | `mimo-v2.6-rl/format-code-task-002552` | `oracle` | 0 | 1 |
| 2026-11-14 | `HAR-438-har438-embedding-cheat-retry` | `har438-embedding-cheat-retry__Menu4KH` | `terminal-bench/embedding-drift-monitor` | `oracle` | 0 | 1 |

The last row is the HAR-438 case.
Harbor captured 0, and the retained reward file holds 1.
A reader that falls back to `reward.txt` reads 1 for a trial that Harbor scored 0.
The first four rows are a 2026-10-09 group with the same direction and the same agent name.
None of the 5 rows is in [contradictions.csv](contradictions.csv), because every captured reward is 0.
The scan compares retained files; it does not identify who wrote either value.

## Patterns and reward sources

The dates with the most rows are 2026-10-18, followed by a tie between 2026-10-15 and 2026-10-19:

| Date | Rows |
|---|---:|
| 2026-10-18 | 39 |
| 2026-10-15 | 18 |
| 2026-10-19 | 18 |
| 2026-10-09 | 5 |
| 2026-10-17 | 5 |
| 2026-10-14 | 2 |
| 2026-10-12 | 1 |
| 2026-11-02 | 1 |
| 2026-11-14 | 1 |

The leading exact task names are:

| Task | Rows |
|---|---:|
| `petermakhnatch/transaction-reconciliation` | 4 |
| `terminal-bench/query-optimize` | 3 |
| `terminal-bench/fix-git` | 2 |

Every other exact task name occurs once.
Rows cluster more strongly by date than by exact task name.
Several task names share prefixes, but this scan does not merge them or infer their common identity.
Neither the date groups nor the task names establish agent intent.

All 90 rows select their reward from `result.json`.
Of these, 89 also have a usable `reward_txt` value; **0 non-empty `reward_txt` values differ from `reward`**.
The remaining row is `bbh-floor-t2t-db-wal-recovery-ca__HH7XBB6`, which has an empty `reward_txt` value.
An empty value is not a reward-source mismatch.
These source counts describe the contradiction rows, not all scanned trials.

## Regenerate and check

Run from the repository worktree:

```bash
python3 research/experiments/reward-report-contradictions/build.py
python3 research/experiments/reward-report-contradictions/build.py --check
```

The first command exits **0** with this output:

```text
scanned=8533 comparable=2235 with_reward=8051 contradictions=90 forged=44 partial-credit=46 unknown=0 mismatches=5
wrote contradictions.csv
wrote reward-file-mismatches.csv
```

The second command exits **0** with this output:

```text
scanned=8533 comparable=2235 with_reward=8051 contradictions=90 forged=44 partial-credit=46 unknown=0 mismatches=5
contradictions.csv: reproduces from the results root
reward-file-mismatches.csv: reproduces from the results root
```

A separate read-only CSV count confirmed the class, agent, task, date, reward-source, line, and byte counts above.
It also checked unique row identities, each row's contradiction rule and classification, and each mismatch row's threshold and agent name.
That scratch check exited **0**.
The check did not regenerate or modify the frozen CSVs.

`--check` rebuilds both CSVs in memory, compares bytes, and never writes.
It exits 1 on any drift in either CSV, including a missing CSV; exit 2 means unusable inputs.
Those are the command's documented exit meanings, not additional checks reported here.
`EVALLAB_RESULTS_ROOT` overrides the default results root.
`--root` selects a results copy; `--out-dir` selects a CSV directory.
The scan never writes to the results root.

## Limits

- The CSVs are a snapshot of the results root on 2026-11-14.
  Newly published trials make `--check` fail until regeneration.
- Only the three-level date/job/trial layout is scanned.
  Deeper layouts and job-level copies are excluded.
- Trials without a usable CTRF report are not comparable and are not rows.
- A missing reward is not a contradiction.
- `forged` states what the test report can support; it does not prove intent.
- `partial-credit` rows are not established defects.
  A weighted, subset, or full-credit scoring model can explain their rewards.
- The scan compares retained files, not an independently observed execution.
  It does not identify who wrote a reward or establish that the report itself is authentic.
- The mismatch CSV reads `verifier/reward.txt` only.
  It does not read `verifier/reward.json`, and it does not report a trial whose captured reward is missing.
- Agent and model names come from `result.json` alone.
  This snapshot has no `model_name` for control agents, so its `model` column is empty.
- The scan never writes to the results root and runs no trial.
