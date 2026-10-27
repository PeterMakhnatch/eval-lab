# TW-3 Hackbook QC protocol

Static, passive measurement of the Hackbook runtime rules (`hb_*`) against
source-labelled DEV traces. No trace code, shell command, Docker container, or
model is executed; traces are read and scanned in memory only.

## Tune split

- **Goodfire DEV** — 200 traces from the pinned cohort in
  `eval-lab-results/2026-10-25/tripwires-tw2-preflight/` (manifest SHA-256
  `f58e4e46…`, admission re-verified against the cloud trace DB read-only):
  100 publisher-enacted positives (`verdict_enacted=yes`) and 100 clean
  controls (`verdict=no, verdict_enacted=no`), one trace per task within
  each label stratum, ranked by `sha256("tw3-hackbook-qc:" + trace_id)`.
  A task can occur in both strata. Rates measure publisher-label agreement,
  not independent ground truth.
- **TB4 DEV** — census memberships from `labels-dev.jsonl`: all 14
  `pass:known_exploit` positives and 86 hash-ranked `pass:neither` source
  negatives ("no documented mechanism found", not a clean certificate).
  DEV tasks are guarded by `tb4-audit-v1` assignments; Hub index TEST lines
  are dropped unparsed.
- **MiMo DEV** — unavailable to this study: no validated DEV-only
  behavioural-label and digest-bound trace-locator projection was identified.
  The standard MiMo trace keys are TEST-only and cannot substitute.

## QC policy

Per runtime rule: firing rate, precision, recall, clean false-alarm rate, and
all-rule **Marginal True Positives** (positives only that rule catches).
Pruning uses a separate **verdict-only marginal** count: diagnostic co-fires
must not erase an alert's unique coverage. With both labelled strata present,
a verdict-eligible rule (`gating_alert`) with clean false-alarm rate **> 10%**
and **0** verdict-only marginal true positives is automatically included in
the emitted frozen `evallab.hackbook_qc_policy/v1` demotion policy. Unknown
denominators are null. Diagnostic rules are not promoted or demoted further.

`evallab hackbook qc --policy-out POLICY.json` writes that policy; the Python
offline scanner consumes it with `scan_trace(..., qc_policy=policy)`.
Demoted hits remain in the report with `eligibility=diagnostic` and `gate=false`,
but no longer emit Hackbook findings. A stale metadata digest or invalid
demotion ID is rejected. This does not deploy a policy to live monitors.

Before/after precision/recall use the **verdict union** (traces fired on by
at least one verdict-eligible rule): before = original eligibility, after =
pruned eligibility, over identical retained fired-rule sets.

## Cross-test

The historical run measured the unchanged runtime matcher on **CheatBench DEV**
and **HackTrace DEV** (150 items each; source `expected_verdict` fail = positive,
pass = clean). Its metadata-only `ruleset_sha256` is not a source-code or adapter
attestation. Reports use `evallab.hackbook_qc/v1` and
`evallab.tune_cross_test/v1`, matching the dashboard count contracts.

## Acceptance limits of the retained run

The 52 runtime rules are not a one-to-one enumeration of all 57 catalog entries.
Task-surface TS04/TS05 matchers were not evaluated against labelled task snapshots;
fleet-rate suppression alone does not meet that requested QC criterion. A
validated MiMo DEV behavioural-label/locator projection is also unavailable to
this study. These gaps preclude a full TW-3 completion claim.

The old Goodfire adapter selected the publisher's final path, including rendered
carrier messages, and its input digest omitted tool calls and reasoning. Treat
those retained tune measurements as preliminary. The full-cohort experiment in
`../pg-self1-goodfire-20261026/run.py` instead validates original/carrier aliases,
preserves original sampled messages, binds observations by ancestry, and hashes
the complete allowlisted actor projection. Do not reuse mixed-split answer-key or
inventory loaders from the historical runner for a new strict-DEV study.

The historical cross-test loader calls `load_answer_key` before filtering DEV,
so its source-read path does not meet the zero-TEST-label-read contract. Later
recounts use only the retained DEV fired-set files, not that loader; they do not
retroactively certify the historical intake or constitute new unseen validation.

## Strict Goodfire full-cohort command

The pinned receipt update admits only an audited addition of publisher passage
annotations; original preflight evidence is preserved. The scanner never receives
those annotations. A fresh output directory is required on every invocation.

```bash
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run python \
  research/experiments/pg-self1-goodfire-20261026/run.py \
  --db ~/.local/state/evallab-prod-live/cloud-trace-db.sqlite \
  --preflight ~/Developer/eval-lab-results/2026-10-26/pg-self1-goodfire-preflight \
  --out ~/Developer/eval-lab-results/2026-10-26/pg-self1-goodfire-new-pass
```

## Historical commands — not a strict-DEV intake recipe

```bash
uv run python research/experiments/tw3-hackbook-qc-20261026/run.py tune \
  --out ~/Developer/eval-lab-results/2026-10-26/tw3-hackbook-qc/tune \
  --manifest ~/Developer/eval-lab-results/2026-10-25/tripwires-tw2-preflight/goodfire-selected-metadata.jsonl \
  --admission-sql ~/Developer/eval-lab-results/2026-10-25/tripwires-tw2-preflight/goodfire-admission-query.sql \
  --cloud-db ~/.local/state/evallab-prod-live/cloud-trace-db.sqlite \
  --hub-index ~/Developer/eval-lab-results/tb4-hub/trajectory-index.jsonl

uv run python research/experiments/tw3-hackbook-qc-20261026/run.py crosstest \
  --tune-dir ~/Developer/eval-lab-results/2026-10-26/tw3-hackbook-qc/tune \
  --out ~/Developer/eval-lab-results/2026-10-26/tw3-hackbook-qc/cross \
  --results-root ~/Developer/eval-lab-results
```
