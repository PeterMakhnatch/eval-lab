# HAR-131 failure atlas — CORRECTED snapshot (historical, pinned)

Supersedes atlas.json `292aee54ea435a189ebb858f619f5654069bd6b22873972039c523976078e6f6` from commit `4aa26712`; its 90-Python denominator was incorrect.
Correction: 9 HAR81 trials (000240×4 and 000434×1 JavaScript; 001520×4 Unknown) are now excluded by requiring canonical Python-ledger membership. Historical 165 native pairs are unchanged: 81 positively identified Python trials, 84 excluded. All nine excluded identities remain inspectable. The admitted `:har129` adapter is eligible for future comparisons, not present in this historical population. G2 and later inputs are outside this pinned snapshot. See `verification.json` and `job-manifest.json`.

---

# HAR-131 failure atlas (inspection-only)

Generated 2026-10-01T09:15:15+00:00 from pinned historical trace-query rows. Every number below is computed from recorded inputs; unknown stays unknown and `counts_verdict` is the sole counted authority. Overlapping patterns are not an exhaustive causal partition.

## Corpus

- Discovered trials: 165; Python-eligible: 81 (recorded MiMo-V2.6-Distill-Qwen-9B (base or admitted :har129 adapter) AND task identity present in the canonical Python ledger; a format-code-task name alone is not a language label)
- Excluded (reported, not dropped): 84 (model or canonical Python identity predicate failed; unknown identity is not asserted non-Python)
- Coverage: missing_processed=47, missing_counts=47, missing_atif=32
- Missing evidence is retained explicitly. Rebuild after report or label refreshes; do not interpret missing counts or labels as clean outcomes.
- The HAR-119 page-calibration freeze independently verifies 24 rater files; heuristic label rows dropped: 0. The query view also verifies 10 HAR-109 hand-label files and the 9-row HAR-128 SFT-pass gate, which is a different taxonomy.
- The later 80-file HAR-128/HAR-116 rater cohort is explicitly unavailable in pinned data revision `26073dfb`. It is not silently backfilled here. Its separate actual runtime proof is `research/experiments/har117-results-home/har131-label-join-proof.json`: 40 trials, 35 rater-agreed cells, 28 matching page predictions.
- Step links open retained local ATIF files and name the raw step_id. Verification checks file bytes and reference existence, not causal responsibility. Context-only anchors and page-opinion anchors remain explicitly labelled.

## Patterns (frequency | eligible denominator | N, current view only)

### recorded-budget-stop [fact; two verified exemplars]
- 54/81 (eligible trials (stop_reason is always recorded; 'unknown' stays separate))
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
- 9/77 (eligible trials with a recorded loop-kind prediction)
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
- 30/77 (eligible trials with a recorded loop-kind prediction)
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
- 6/77 (eligible trials with counts_verdict non-null)
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
- 11/77 (eligible trials with counts_verdict non-null)
- Predicate: `counts_verdict == 'excluded' with reason infra (canonical counts)`
- [`har110-dev-002256-cfe31418__uDRp4Vb` / head#15](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-har110-dev-002256-cfe31418/har110-dev-002256-cfe31418__uDRp4Vb/agent/trajectory.json#step_id=15>) (recorded-command, reference-verified=True). Selection: loop-onset opinion head#15. [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-09-30/HAR-110-har110-dev-002256-cfe31418/har110-dev-002256-cfe31418__uDRp4Vb/result.json>).
```text
pwd

```
- [`har116-a-000383-loopfix__wgujpBQ` / head#1](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-116-har116-a-000383-loopfix/har116-a-000383-loopfix__wgujpBQ/agent/trajectory.json#step_id=1>) (none, reference-verified=True). Selection: median real non-copied step (no recorded ref resolved). [Native result](<file:///Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/HAR-116-har116-a-000383-loopfix/har116-a-000383-loopfix__wgujpBQ/result.json>).
  Setup-context anchor only; no recorded command or observation at this step.
- Lever (hypothesis): Infra reliability (harness/sandbox), not model capability; kept out of pass-rate denominator.

### recorded-upstream-fetch-signal [fact-signal; two verified exemplars]
- 13/77 (eligible trials with processed_available (taint lives in processed reports))
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

The following rebuilds the **current** corpus, not this frozen population:

`PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py --repo-root . --out-dir research/explorations/trace-lab/failure-atlas`

To replay this historical snapshot, use a data checkout at `26073dfb7d831d964ea430ee7f59099f2526279a` for `--repo-root`, the recorded results home and `derived/parquet` root, and pass every `job_dirs` value in `job-manifest.json` as a repeated `--job-dir`. That pins 172 physical job copies to the same 165 native identities. It requires the retained local run files; this repository does not contain those raw inputs.

The pinned API records `published_job_dir=null`, so its full projection digest differs from the earlier discovery-mode digest. The Python-only correction at `a6468126` preserved all ten query row sets and the atlas input digest. The subsequent cohort-aware SQL revision adds a `cohort` column and explicitly non-additive study rows to query 08; the other nine query outputs remain unchanged. No G2 or later frozen-label cohort is silently inserted into this historical receipt.
