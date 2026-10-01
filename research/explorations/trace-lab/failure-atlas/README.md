# HAR-131 failure atlas (inspection-only)

Generated 2026-10-01T07:59:43+00:00 from current trace-query rows. Every number below is computed from recorded inputs; unknown stays unknown and `counts_verdict` is the sole counted authority. Overlapping patterns are not an exhaustive causal partition.

## Corpus

- Discovered trials: 165; Python-eligible: 90 (model_name matches MiMo-V2.6-Distill-Qwen-9B AND task family format-code-task-NNNNNN (recorded identity, ledger corroborates family=Python))
- Excluded (reported, not dropped): 75 (non-Python family or non-MiMo model; see atlas.json)
- Coverage: missing_processed=47, missing_counts=47, missing_atif=32
- Missing evidence is retained explicitly. Rebuild after report or label refreshes; do not interpret missing counts or labels as clean outcomes.
- Frozen labels independently re-verified (24 files ok=True); heuristic label rows dropped: 0.
- Step links open retained local ATIF files and name the raw step_id. Verification checks file bytes and reference existence, not causal responsibility. Context-only anchors and page-opinion anchors remain explicitly labelled.

## Patterns (frequency | eligible denominator | N, current view only)

### recorded-budget-stop [fact; two verified exemplars]
- 61/90 (eligible trials (stop_reason is always recorded; 'unknown' stays separate))
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
- 12/86 (eligible trials with a recorded loop-kind prediction)
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
- 35/86 (eligible trials with a recorded loop-kind prediction)
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
- 6/86 (eligible trials with counts_verdict non-null)
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
- 11/86 (eligible trials with counts_verdict non-null)
- Predicate: `counts_verdict == 'excluded' with reason infra (canonical counts)`
- [`har110-dev-002256-cfe31418__uDRp4Vb` / head#15](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-har110-dev-002256-cfe31418/har110-dev-002256-cfe31418__uDRp4Vb/agent/trajectory.json#step_id=15>) (recorded-command, reference-verified=True). Selection: loop-onset opinion head#15. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-har110-dev-002256-cfe31418/har110-dev-002256-cfe31418__uDRp4Vb/result.json>).
```text
pwd

```
- [`har116-a-000383-loopfix__wgujpBQ` / head#1](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-116-har116-a-000383-loopfix/har116-a-000383-loopfix__wgujpBQ/agent/trajectory.json#step_id=1>) (none, reference-verified=True). Selection: median real non-copied step (no recorded ref resolved). [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-116-har116-a-000383-loopfix/har116-a-000383-loopfix__wgujpBQ/result.json>).
  Setup-context anchor only; no recorded command or observation at this step.
- Lever (hypothesis): Infra reliability (harness/sandbox), not model capability; kept out of pass-rate denominator.

### recorded-upstream-fetch-signal [fact-signal; two verified exemplars]
- 13/86 (eligible trials with processed_available (taint lives in processed reports))
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

- ledger-discarded-task: Canonical ledger-discarded task identities are task-health exclusions, not pooled model failures. `har104-d-002259__cptLF6h`, `har104-d-002407__LRiiKmy`
- unscored-unknown: Eligible trials with no verifier reward (scored=False). Unknown is NOT infra or earned; kept visible in denominators, never in pass/fail rates. Overlaps counts-excluded-infra for the infra-excluded trials by design (overlapping accounting, not a causal partition). `har104-d-000226__Y2BTVP3`, `har104-d-000226__v276NfY`, `har104-d-001896__BQr7N9b`, `har104-d-001896__bRWKiLq`, `har110-dev-002256-cfe31418__uDRp4Vb`, `har116-a-000383-loopfix__wgujpBQ`, `har116-a-000495-loopfix__rXR8je7`, `har116-a-000587-loopfix__X5SAhvg`

## Task health (ledger)

Ledger-discarded/review tasks are authoritative task signals, not model failures. Full per-task table in atlas.json.

## GEPA reflection firewall

- Status: unavailable -- no authorized G2 training bindings supplied (--g2-bindings absent); reflection unavailable.
- Payload file: none (unavailable)

## Refresh

`PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py --repo-root . --out-dir research/explorations/trace-lab/failure-atlas`
