# HAR-131 failure atlas (inspection-only, PROVISIONAL)

Generated 2026-10-01T05:18:17+00:00 from current trace-query rows. PROVISIONAL: the query surface is under independent-review fix and published counts/pages are stale pending the parent reprocess -- no finished atlas claim until the fix and parent refresh land. Every number below is computed from current rows; unknown stays unknown and `counts_verdict` is the sole counted authority. Overlapping patterns are not an exhaustive causal partition.

## Corpus

- Discovered trials: 158; Python-eligible: 83 (model_name matches MiMo-V2.6-Distill-Qwen-9B AND task family format-code-task-NNNNNN (recorded identity, ledger corroborates family=Python))
- Excluded (reported, not dropped): 75 (non-Python family or non-MiMo model; see atlas.json)
- Coverage: missing_processed=103, missing_counts=118, missing_atif=32
- Published counts/pages are stale for HAR-81/85/90/104/110 (counts only on HAR-116); parent reprocesses post-PR609 -- re-run the refresh command in atlas.json.
- Frozen labels independently re-verified (24 files ok=True); heuristic label rows dropped: 0.

## Patterns (frequency | eligible denominator | N, current view only)

### recorded-budget-stop [fact; generalized]
- 55/83 (eligible trials (stop_reason is always recorded; 'unknown' stays separate))
- Predicate: `stop_reason in {TrialBudgetExhaustedError, ceiling:input_tokens, ceiling:requests}`
- `har104-d-000226__JCDfZFi` head#121 (agent/trajectory.json, recorded-command, raw-verified=True) :: echo complete

- `har104-d-000383__PmZMZ6z` head#68 (agent/trajectory.json, recorded-command, raw-verified=True) :: mark_task_complete
- Lever (hypothesis): Harness budget/ceiling policy or agent time-to-first-edit efficiency; causal evidence absent.

### completion-claim-loop [opinion; generalized]
- 3/40 (eligible trials with loop_kind non-null (decision v2 present))
- Predicate: `decision loop_kind.kind == 'completion-claim' (producer rule HAR-119)`
- Opinion limits: loop rule 7/11 vs rater-agreed (HAR-119 only); first_failure usually absent; blame near-constant prior.
- `har116-a-002864-baseline__LnaWDq6` head#22 (agent/trajectory.json, recorded-command, raw-verified=True) :: true

- `har116-b-000146-original__JT6tRiN` head#59 (agent/trajectory.json, observation-only, raw-verified=True) :: Previous response had parsing errors:
ERROR: No valid JSON found in response
WARNINGS: - No valid JSON object found

Ple
- Lever (hypothesis): Prompt/harness completion discipline (e.g. confirm-then-stop); causal evidence absent.

### repetition-loop [opinion; generalized]
- 12/40 (eligible trials with loop_kind non-null (decision v2 present))
- Predicate: `decision loop_kind.kind == 'repetition' (producer rule HAR-119)`
- Opinion limits: loop rule 7/11 vs rater-agreed (HAR-119 only); first_failure usually absent; blame near-constant prior.
- `har116-a-000383-loopfix-r2__cNEHjBa` head#56 (agent/trajectory.json, recorded-command, raw-verified=True) :: python3 - <<'EOF'
import quickfix as fix
names = [n for n in dir(fix) if not n.startswith('_')]
for n in names:
    if '
- `har116-a-000495-baseline__DiGmEPC` head#64 (agent/trajectory.json, recorded-command, raw-verified=True) :: cd /testbed && python3 - <<'EOF'
from cfnlint.template import Template
from cfnlint.jsonschema.validators import CfnTemp
- Lever (hypothesis): Harness loop-break / output-cap or agent stuckness recovery; causal evidence absent.

### counts-excluded-copied-pass [fact; generalized]
- 3/40 (eligible trials with counts_verdict non-null)
- Predicate: `counts_verdict == 'excluded' with reason copied_fix or pass_tainted (canonical counts)`
- `har116-b-000927-original__kvyP9fr` head#66 (agent/trajectory.json, observation-only, raw-verified=True) :: Previous response had parsing errors:
ERROR: No valid JSON found in response
WARNINGS: - No valid JSON object found

Ple
- `har116-b-002308-leakclosed__idknFAu` head#14 (agent/trajectory.json, recorded-command, raw-verified=True) :: pip download pre-commit==2.15.0 --no-deps -d /tmp/pc 2>&1 | tail -2; ls /tmp/pc 2>/dev/null

grep -rn "post-rewrite\|rew
- Lever (hypothesis): Data integrity: keep excluded from training; upstream-fetch guard is a hypothesis.

### counts-excluded-infra [fact; generalized]
- 10/40 (eligible trials with counts_verdict non-null)
- Predicate: `counts_verdict == 'excluded' with reason infra (canonical counts)`
- `har116-a-000383-loopfix__wgujpBQ` head#1 (agent/trajectory.json, reconstructed-command (not proof of execution), raw-verified=True) :: ls -la; cd project
- `har116-a-000495-loopfix__rXR8je7` head#1 (agent/trajectory.json, reconstructed-command (not proof of execution), raw-verified=True) :: ls -la; cd project
- Lever (hypothesis): Infra reliability (harness/sandbox), not model capability; kept out of pass-rate denominator.

### recorded-upstream-fetch-signal [fact-signal; generalized]
- 9/55 (eligible trials with processed_available (taint lives in processed reports))
- Predicate: `processed taint contains an upstream_fetch entry (recorded command, NOT proof of copying)`
- `har110-001161-plain__RZ8qyUu` head#27 (agent/trajectory.json, recorded-command, raw-verified=True) :: pip download docformatter==1.7.1 --no-deps -d /tmp/dfdl 2>&1 | tail -3

- `har110-001181-plain__AnjbfwU` head#12 (agent/trajectory.json, recorded-command, raw-verified=True) :: cd /testbed && git log --all --oneline | head -20 && git branch -a && git tag | tail -5

pip download rich==13.7.0 --no-
- Lever (hypothesis): Fetch command != fetched solution; treat as audit signal only.

## Rare cases

- ledger-discarded-task: Trial(s) on ledger-discarded (authoritatively task-broken) tasks; n<2 stays a rare case, never a generalized category. `har104-d-002259__cptLF6h`
- unscored-unknown: Eligible trials with no verifier reward (scored=False). Unknown is NOT infra or earned; kept visible in denominators, never in pass/fail rates. Overlaps counts-excluded-infra for the infra-excluded trials by design (overlapping accounting, not a causal partition). `har104-d-000226__Y2BTVP3`, `har104-d-000226__v276NfY`, `har104-d-001896__BQr7N9b`, `har104-d-001896__bRWKiLq`, `har110-dev-002256-cfe31418__uDRp4Vb`, `har116-a-000383-loopfix__wgujpBQ`, `har116-a-000495-loopfix__rXR8je7`, `har116-a-000587-loopfix__X5SAhvg`

## Task health (ledger)

Ledger-discarded/review tasks are authoritative task signals, not model failures. Full per-task table in atlas.json.

## GEPA reflection firewall

- Status: unavailable -- no authorized G2 training bindings supplied (--g2-bindings absent); G2 has not landed. Unavailable reflection is a dependency status, not an empty training atlas.

## Refresh

`PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py --repo-root . --out-dir research/explorations/trace-lab/failure-atlas`
