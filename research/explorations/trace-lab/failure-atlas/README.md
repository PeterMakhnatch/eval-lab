# HAR-131 failure atlas (inspection-only)

Generated 2026-10-01T17:07:48+00:00 from current trace-query rows. Every number below is computed from recorded inputs; unknown stays unknown and `counts_verdict` is the sole counted authority. Overlapping patterns are not an exhaustive causal partition.

## Corpus

- Discovered trials: 296; Python-eligible: 212 (recorded MiMo-V2.6-Distill-Qwen-9B (base or admitted :har129 adapter) AND task identity present in the canonical Python ledger; a format-code-task name alone is not a language label)
- Excluded (reported, not dropped): 84 (model or canonical Python identity predicate failed; unknown identity is not asserted non-Python)
- Coverage: missing_processed=47, missing_counts=47, missing_atif=33
- Missing evidence is retained explicitly. Rebuild after report or label refreshes; do not interpret missing counts or labels as clean outcomes.
- Frozen labels independently re-verified (24 files ok=True); heuristic label rows dropped: 0.
- Step links open retained local ATIF files and name the raw step_id. Verification checks file bytes and reference existence, not causal responsibility. Context-only anchors and page-opinion anchors remain explicitly labelled.

## Recorded G5 stock / tuned / GEPA (separate frozen cohort)

- Cohort: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/research/experiments/ovn-sft-v0/g5-specs/cohort.json` (`sha256:ecfb2613ecfe0fdc941b28a07fdf67fc0ee8110bf05234c758b244330bce80d3`).
- Frozen 60-spec manifest: `sha256:a8551c185172e7ad5b6c54acd50b4e41a8fd4a7d558cf6992e819b22c8838029`.
- Observed state: `recorded_snapshot`. Submission or rejection bookkeeping is not a native outcome.
- Only exact frozen G5 task/arm/job/spec bindings; historical trials are not pooled. Counts missing includes not_run, unknown and unavailable/ambiguous bindings, not counted failures.
- GEPA: stock weights plus `sha256:b55a90cdf5e07719150c5642043bebfa54ce7ff68f28aff2009d4f738e7f9470`; arm identity comes from recorded metadata and exact spec binding, not the base-model name.
- Calibration: unavailable; No frozen G5 calibration receipt supplied; historical accuracy is not G5 accuracy.

| Arm | Expected | Recorded/bound | Pass | Fail | Excluded | Missing | Not run | Unknown | Binding unavailable | Counted denominator |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| stock | 20 | 20 | 2 | 15 | 3 | 0 | 0 | 0 | 0 | 17 |
| tuned | 20 | 20 | 1 | 18 | 1 | 0 | 0 | 0 | 0 | 19 |
| gepa | 20 | 20 | 3 | 16 | 1 | 0 | 0 | 0 | 0 | 19 |


- stock evidence / 20 expected: processed=20, counts=20, ATIF=20, loop predictions=20, scored=17.

- tuned evidence / 20 expected: processed=20, counts=20, ATIF=20, loop predictions=20, scored=19.

- gepa evidence / 20 expected: processed=20, counts=20, ATIF=20, loop predictions=20, scored=19.

### G5 existing-pattern frequencies (available evidence denominators)

- stock / recorded-budget-stop: 13/20; 0 expected cells outside evidence denominator (eligible trials (stop_reason is always recorded; 'unknown' stays separate)).
  - [`ovn-g5-000169-stock__npH4TqV` / head#88](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-stock/ovn-g5-000169-stock__npH4TqV/agent/trajectory.json#step_id=88>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-000332-stock__Xn3uN4Q` / head#101](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000332-stock/ovn-g5-000332-stock__Xn3uN4Q/agent/trajectory.json#step_id=101>), recorded-command; raw-reference-verified=True.
- stock / completion-claim-loop: 2/20; 0 expected cells outside evidence denominator (eligible trials with a recorded loop-kind prediction).
  - [`ovn-g5-001809-stock__PZ8sw7f` / head#53](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001809-stock/ovn-g5-001809-stock__PZ8sw7f/agent/trajectory.json#step_id=53>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-001833-stock__UTVAfnN` / head#51](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001833-stock/ovn-g5-001833-stock__UTVAfnN/agent/trajectory.json#step_id=51>), observation-only; raw-reference-verified=True.
- stock / repetition-loop: 14/20; 0 expected cells outside evidence denominator (eligible trials with a recorded loop-kind prediction).
  - [`ovn-g5-000332-stock__Xn3uN4Q` / head#24](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000332-stock/ovn-g5-000332-stock__Xn3uN4Q/agent/trajectory.json#step_id=24>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-000521-stock__pjKC2mS` / head#34](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000521-stock/ovn-g5-000521-stock__pjKC2mS/agent/trajectory.json#step_id=34>), recorded-command; raw-reference-verified=True.
- stock / counts-excluded-copied-pass: 0/20; 0 expected cells outside evidence denominator (eligible trials with counts_verdict non-null).
- stock / counts-excluded-infra: 3/20; 0 expected cells outside evidence denominator (eligible trials with counts_verdict non-null).
  - [`ovn-g5-001241-stock__FGtdyNH` / head#17](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001241-stock/ovn-g5-001241-stock__FGtdyNH/agent/trajectory.json#step_id=17>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-001626-stock__6P6YXKD` / head#15](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001626-stock/ovn-g5-001626-stock__6P6YXKD/agent/trajectory.json#step_id=15>), recorded-command; raw-reference-verified=True.
- stock / recorded-upstream-fetch-signal: 3/20; 0 expected cells outside evidence denominator (eligible trials with processed_available (taint lives in processed reports)).
  - [`ovn-g5-001136-stock__jjgSG57` / head#47](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001136-stock/ovn-g5-001136-stock__jjgSG57/agent/trajectory.json#step_id=47>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-001695-stock__PveqguL` / head#7](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001695-stock/ovn-g5-001695-stock__PveqguL/agent/trajectory.json#step_id=7>), recorded-command; raw-reference-verified=True.
- tuned / recorded-budget-stop: 12/20; 0 expected cells outside evidence denominator (eligible trials (stop_reason is always recorded; 'unknown' stays separate)).
  - [`ovn-g5-000169-tuned__CxBpceW` / head#100](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-tuned/ovn-g5-000169-tuned__CxBpceW/agent/trajectory.json#step_id=100>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-000521-tuned__TdWLAib` / head#109](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000521-tuned/ovn-g5-000521-tuned__TdWLAib/agent/trajectory.json#step_id=109>), recorded-command; raw-reference-verified=True.
- tuned / completion-claim-loop: 2/20; 0 expected cells outside evidence denominator (eligible trials with a recorded loop-kind prediction).
  - [`ovn-g5-001833-tuned__rNpMkcb` / head#48](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001833-tuned/ovn-g5-001833-tuned__rNpMkcb/agent/trajectory.json#step_id=48>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-002302-tuned__5j77iHJ` / head#69](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-002302-tuned/ovn-g5-002302-tuned__5j77iHJ/agent/trajectory.json#step_id=69>), recorded-command; raw-reference-verified=True.
- tuned / repetition-loop: 11/20; 0 expected cells outside evidence denominator (eligible trials with a recorded loop-kind prediction).
  - [`ovn-g5-000332-tuned__zevQhXu` / head#82](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000332-tuned/ovn-g5-000332-tuned__zevQhXu/agent/trajectory.json#step_id=82>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-000521-tuned__TdWLAib` / head#83](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000521-tuned/ovn-g5-000521-tuned__TdWLAib/agent/trajectory.json#step_id=83>), recorded-command; raw-reference-verified=True.
- tuned / counts-excluded-copied-pass: 0/20; 0 expected cells outside evidence denominator (eligible trials with counts_verdict non-null).
- tuned / counts-excluded-infra: 1/20; 0 expected cells outside evidence denominator (eligible trials with counts_verdict non-null).
  - [`ovn-g5-000332-tuned__zevQhXu` / head#82](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000332-tuned/ovn-g5-000332-tuned__zevQhXu/agent/trajectory.json#step_id=82>), recorded-command; raw-reference-verified=True.
- tuned / recorded-upstream-fetch-signal: 2/20; 0 expected cells outside evidence denominator (eligible trials with processed_available (taint lives in processed reports)).
  - [`ovn-g5-000169-tuned__CxBpceW` / head#20](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-tuned/ovn-g5-000169-tuned__CxBpceW/agent/trajectory.json#step_id=20>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-001809-tuned__AUVzrvM` / head#63](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001809-tuned/ovn-g5-001809-tuned__AUVzrvM/agent/trajectory.json#step_id=63>), recorded-command; raw-reference-verified=True.
- gepa / recorded-budget-stop: 15/20; 0 expected cells outside evidence denominator (eligible trials (stop_reason is always recorded; 'unknown' stays separate)).
  - [`ovn-g5-000169-gepa__Hmxf7wb` / head#101](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000169-gepa/ovn-g5-000169-gepa__Hmxf7wb/agent/trajectory.json#step_id=101>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-000521-gepa__2rqo6EY` / head#104](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000521-gepa/ovn-g5-000521-gepa__2rqo6EY/agent/trajectory.json#step_id=104>), recorded-command; raw-reference-verified=True.
- gepa / completion-claim-loop: 1/20; 0 expected cells outside evidence denominator (eligible trials with a recorded loop-kind prediction).
  - [`ovn-g5-001809-gepa__nxdBvhh` / head#29](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001809-gepa/ovn-g5-001809-gepa__nxdBvhh/agent/trajectory.json#step_id=29>), observation-only; raw-reference-verified=True.
- gepa / repetition-loop: 14/20; 0 expected cells outside evidence denominator (eligible trials with a recorded loop-kind prediction).
  - [`ovn-g5-000332-gepa__YyfJfPU` / head#58](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000332-gepa/ovn-g5-000332-gepa__YyfJfPU/agent/trajectory.json#step_id=58>), recorded-command; raw-reference-verified=True.
  - [`ovn-g5-000521-gepa__2rqo6EY` / head#5](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-000521-gepa/ovn-g5-000521-gepa__2rqo6EY/agent/trajectory.json#step_id=5>), recorded-command; raw-reference-verified=True.
- gepa / counts-excluded-copied-pass: 0/20; 0 expected cells outside evidence denominator (eligible trials with counts_verdict non-null).
- gepa / counts-excluded-infra: 1/20; 0 expected cells outside evidence denominator (eligible trials with counts_verdict non-null).
  - [`ovn-g5-001833-gepa__aci6NkF` / head#28](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-001833-gepa/ovn-g5-001833-gepa__aci6NkF/agent/trajectory.json#step_id=28>), recorded-command; raw-reference-verified=True.
- gepa / recorded-upstream-fetch-signal: 1/20; 0 expected cells outside evidence denominator (eligible trials with processed_available (taint lives in processed reports)).
  - [`ovn-g5-002017-gepa__qQJWeCc` / head#76](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-002017-gepa/ovn-g5-002017-gepa__qQJWeCc/agent/trajectory.json#step_id=76>), recorded-command; raw-reference-verified=True.

- Binding rejections: 0; identities/reasons and every missing cell are retained in atlas.json.

## Frozen loop-calibration limits (studies remain separate)

- Historical HAR-119 page_scores cohort only: 7/11; not transferred to G5.
- These are frozen agent-rater calibration measurements, not human ground truth or G5 accuracy.
- har128-har116: 28/35 vs agreed raters; 0 abstentions. Receipt `research/experiments/har117-results-home/har131-page-calibration-har116.json` / `sha256:b179b9a7fed89f07638b5f3acf820a2907bc9f05cf9b069d08400e94203da912`; label freeze `sha256:24b91001adf5707ebb65756e575a9acbc39a816173ee16a5da88cf989e99513d`. This study only; not pooled or transferred to G5. Inspection/calibration only, never training reflection.
- har128-g2-a1: 12/17 vs agreed raters; 0 abstentions. Receipt `research/experiments/har117-results-home/har131-page-calibration-g2-a1.json` / `sha256:0b70d7ef5eaba161aa82e8edfbac2ab7c19edcbd514b0b49ae1bf3ccb5241085`; label freeze `sha256:f6a11da4b3994c101785742f27565d169469e062764585a210baeccfb5b92674`. This study only; not pooled or transferred to G5. Inspection/calibration only, never training reflection.
- har128-g2-r2: 10/16 vs agreed raters; 0 abstentions. Receipt `research/experiments/har117-results-home/har131-page-calibration-g2-r2.json` / `sha256:73b861e43482f39686fe56fecde6ace457a75a8243df4006ed89700f22de52f6`; label freeze `sha256:ddc1f2ad8bf52dc762067a367f7383b990164174b1732c108fb788167597f2ff`. This study only; not pooled or transferred to G5. Inspection/calibration only, never training reflection.
- har128-g2-tail: 1/3 vs agreed raters; 0 abstentions. Receipt `research/experiments/har117-results-home/har131-page-calibration-g2-tail.json` / `sha256:f6ae380a7545277e112b9e8eeb7aea52d77f375d1bd1a5de2a9122caa45c13bb`; label freeze `sha256:3b88eb4c2450d76f4b58533c12fae7a0ed25f0816bb45d1b3baeb72a54cdbb50`. This study only; not pooled or transferred to G5. Inspection/calibration only, never training reflection.

## Historical non-G5 patterns (frequency | eligible evidence denominator)

- Scope: 152 eligible rows. Non-G5 eligible rows only; reserved ovn-g5 job identities are kept out, even when G5 binding is unavailable.

### recorded-budget-stop [fact; two verified exemplars]
- 73/152 (eligible trials (stop_reason is always recorded; 'unknown' stays separate))
- Predicate: `stop_reason in {TrialBudgetExhaustedError, ceiling:input_tokens, ceiling:requests}`
- [`gepa-terminus-2-format-code-task__7RJJeFg` / head#76](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-d1471e1b2701d6d1765ff1df/gepa-terminus-2-format-code-task__7RJJeFg/agent/trajectory.json#step_id=76>) (recorded-command, reference-verified=True). Selection: highest native step_id context; not a causal or chronological assertion. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-d1471e1b2701d6d1765ff1df/gepa-terminus-2-format-code-task__7RJJeFg/result.json>).
```text
echo COMPLETE_TASK_AND_STOP

```
- [`gepa-terminus-2-format-code-task__8FqvKUU` / head#88](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-002-a951229932dc90303c90215d/gepa-terminus-2-format-code-task__8FqvKUU/agent/trajectory.json#step_id=88>) (recorded-command, reference-verified=True). Selection: highest native step_id context; not a causal or chronological assertion. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-002-a951229932dc90303c90215d/gepa-terminus-2-format-code-task__8FqvKUU/result.json>).
```text
echo complete

```
- Lever (hypothesis): Harness budget/ceiling policy or agent time-to-first-edit efficiency; causal evidence absent.

### completion-claim-loop [opinion; two verified exemplars]
- 23/148 (eligible trials with a recorded loop-kind prediction)
- Predicate: `decision loop_kind.kind == 'completion-claim' (producer rule HAR-119)`
- Opinion limits (page_scores cohort only): loop 7/11 vs rater-agreed; first_failure 1/9 with 1/12 coverage and 11 abstentions; page values are usually absent -- do not use as step truth; blame 11/11 with 0 abstentions; near-constant prior, not skill.
- [`gepa-terminus-2-format-code-task__7RJJeFg` / head#42](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-d1471e1b2701d6d1765ff1df/gepa-terminus-2-format-code-task__7RJJeFg/agent/trajectory.json#step_id=42>) (recorded-command, reference-verified=True). Selection: loop-onset opinion head#42. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-d1471e1b2701d6d1765ff1df/gepa-terminus-2-format-code-task__7RJJeFg/result.json>).
```text
echo COMPLETE_TASK_AND_STOP

```
- [`gepa-terminus-2-format-code-task__8FqvKUU` / head#43](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-002-a951229932dc90303c90215d/gepa-terminus-2-format-code-task__8FqvKUU/agent/trajectory.json#step_id=43>) (recorded-command, reference-verified=True). Selection: loop-onset opinion head#43. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-002-a951229932dc90303c90215d/gepa-terminus-2-format-code-task__8FqvKUU/result.json>).
```text
echo complete

```
- Lever (hypothesis): Prompt/harness completion discipline (e.g. confirm-then-stop); causal evidence absent.

### repetition-loop [opinion; two verified exemplars]
- 50/148 (eligible trials with a recorded loop-kind prediction)
- Predicate: `decision loop_kind.kind == 'repetition' (producer rule HAR-119)`
- Opinion limits (page_scores cohort only): loop 7/11 vs rater-agreed; first_failure 1/9 with 1/12 coverage and 11 abstentions; page values are usually absent -- do not use as step truth; blame 11/11 with 0 abstentions; near-constant prior, not skill.
- [`gepa-terminus-2-format-code-task__bo5YsYL` / head#25](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-000-9862796a5e5624e818ab5f04/gepa-terminus-2-format-code-task__bo5YsYL/agent/trajectory.json#step_id=25>) (recorded-command, reference-verified=True). Selection: loop-onset opinion head#25. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-000-9862796a5e5624e818ab5f04/gepa-terminus-2-format-code-task__bo5YsYL/result.json>).
```text
python3 -c "
import quickfix as fix
print(fix.MarketDataEntryType_BID.value)
print(fix.MarketDataEntryType_BID.value())

```
- [`har104-d-001896__MDkTErY` / head#33](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-104-har104-d-001896/har104-d-001896__MDkTErY/agent/trajectory.json#step_id=33>) (recorded-command, reference-verified=True). Selection: loop-onset opinion head#33. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-104-har104-d-001896/har104-d-001896__MDkTErY/result.json>).
```text
echo done

```
- Lever (hypothesis): Harness loop-break / output-cap or agent stuckness recovery; causal evidence absent.

### counts-excluded-copied-pass [fact; two verified exemplars]
- 9/148 (eligible trials with counts_verdict non-null)
- Predicate: `counts_verdict == 'excluded' with reason copied_fix or pass_tainted (canonical counts)`
- [`gepa-terminus-2-format-code-task__CFCbfps` / head#20](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-c074cb87ddff7e1368343d33/gepa-terminus-2-format-code-task__CFCbfps/agent/trajectory.json#step_id=20>) (recorded-command, reference-verified=True). Selection: upstream-fetch detector evidence head#20. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-c074cb87ddff7e1368343d33/gepa-terminus-2-format-code-task__CFCbfps/result.json>).
```text
grep -rn "rename" /workspace/repo/docs -l 2>/dev/null | head; pip download siuba==0.4.6 --no-deps -d /tmp/siuba_dl 2>&1 
```
- [`har104-d-000226__JCDfZFi` / head#4](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-104-har104-d-000226/har104-d-000226__JCDfZFi/agent/trajectory.json#step_id=4>) (recorded-command, reference-verified=True). Selection: upstream-fetch detector evidence head#4. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-104-har104-d-000226/har104-d-000226__JCDfZFi/result.json>).
```text
cd /testbed && pip download waitress==2.0.0 --no-deps -d /tmp/wtr 2>&1 | tail -2; ls /tmp/wtr 2>/dev/null

cd /testbed &
```
- Lever (hypothesis): Data integrity: keep excluded from training; upstream-fetch guard is a hypothesis.

### counts-excluded-infra [fact; two verified exemplars]
- 34/148 (eligible trials with counts_verdict non-null)
- Predicate: `counts_verdict == 'excluded' with reason infra (canonical counts)`
- [`har110-dev-002256-cfe31418__uDRp4Vb` / head#15](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-har110-dev-002256-cfe31418/har110-dev-002256-cfe31418__uDRp4Vb/agent/trajectory.json#step_id=15>) (recorded-command, reference-verified=True). Selection: loop-onset opinion head#15. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-har110-dev-002256-cfe31418/har110-dev-002256-cfe31418__uDRp4Vb/result.json>).
```text
pwd

```
- [`har116-a-000383-loopfix__wgujpBQ` / head#1](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-116-har116-a-000383-loopfix/har116-a-000383-loopfix__wgujpBQ/agent/trajectory.json#step_id=1>) (none, reference-verified=True). Selection: median real non-copied step (no recorded ref resolved). [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-116-har116-a-000383-loopfix/har116-a-000383-loopfix__wgujpBQ/result.json>).
  Setup-context anchor only; no recorded command or observation at this step.
- Lever (hypothesis): Infra reliability (harness/sandbox), not model capability; kept out of pass-rate denominator.

### recorded-upstream-fetch-signal [fact-signal; two verified exemplars]
- 20/148 (eligible trials with processed_available (taint lives in processed reports))
- Predicate: `processed taint contains an upstream_fetch detector entry; command provenance is separate, not proof of execution or copying`
- [`gepa-terminus-2-format-code-task__CFCbfps` / head#20](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-c074cb87ddff7e1368343d33/gepa-terminus-2-format-code-task__CFCbfps/agent/trajectory.json#step_id=20>) (recorded-command, reference-verified=True). Selection: upstream-fetch detector evidence head#20. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-gepa-terminus-2-format-code-task-001-c074cb87ddff7e1368343d33/gepa-terminus-2-format-code-task__CFCbfps/result.json>).
```text
grep -rn "rename" /workspace/repo/docs -l 2>/dev/null | head; pip download siuba==0.4.6 --no-deps -d /tmp/siuba_dl 2>&1 
```
- [`har104-d-000226__JCDfZFi` / head#4](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-104-har104-d-000226/har104-d-000226__JCDfZFi/agent/trajectory.json#step_id=4>) (recorded-command, reference-verified=True). Selection: upstream-fetch detector evidence head#4. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-104-har104-d-000226/har104-d-000226__JCDfZFi/result.json>).
```text
cd /testbed && pip download waitress==2.0.0 --no-deps -d /tmp/wtr 2>&1 | tail -2; ls /tmp/wtr 2>/dev/null

cd /testbed &
```
- Lever (hypothesis): Fetch command != fetched solution; treat as audit signal only.

## Rare cases

- ledger-discarded-task: Canonical ledger-discarded task identities are task-health exclusions, not pooled model failures. `har104-d-002259__cptLF6h`, `har104-d-002407__LRiiKmy`, `har120-001269-a1__y3Wy5Ku`, `har120-001269-a2-r2__93WpuaE`, `har120-001269-a2__sA8cq7s`
- unscored-unknown: Eligible trials with no verifier reward (scored=False). Unknown is NOT infra or earned; kept visible in denominators, never in pass/fail rates. Overlaps counts-excluded-infra for the infra-excluded trials by design (overlapping accounting, not a causal partition). `har104-d-000226__Y2BTVP3`, `har104-d-000226__v276NfY`, `har104-d-001896__BQr7N9b`, `har104-d-001896__bRWKiLq`, `har110-dev-002256-cfe31418__uDRp4Vb`, `har116-a-000383-loopfix__wgujpBQ`, `har116-a-000495-loopfix__rXR8je7`, `har116-a-000587-loopfix__X5SAhvg`

## Task health (ledger)

Ledger-discarded/review tasks are authoritative task signals, not model failures. Full per-task table in atlas.json.

## GEPA reflection firewall

- Status: unavailable -- no authorized G2 training bindings supplied (--g2-bindings absent); reflection unavailable.
- Payload file: none (unavailable)

## Refresh

`PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py --repo-root . --out-dir research/explorations/trace-lab/failure-atlas --g5-cohort /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/research/experiments/ovn-sft-v0/g5-specs/cohort.json`
