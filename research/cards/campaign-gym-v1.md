# Campaign card — gym-v1 baseline, wave 1

**Status: PRE-REGISTERED / PREPARED.** This card records the baseline campaign definition
for frozen generation `gym-v1` (4 human-approved registered tasks). It reports **no rates,
no intervals, and no findings** because trials have not been scored. Nothing in this file
may be cited as a completed comparative result.

## Question

Does the gym's frozen task set (`gym-v1`) produce a stable baseline pass rate across
registered task families for evaluated agents, with free `oracle` and `nop` controls per
task confirming the instruments read true?

Purpose: `baseline`. All results cite the frozen manifest
`library/frozen/gym-v1/manifest.json` so that comparisons across time are reproducible.

## Configuration and evidence

| Field | Value |
|---|---|
| Frozen set | `gym-v1` |
| Registered tasks in `gym-v1` | **4** (`event-summary`, `query-optimize`, `terminal-bench-html-js-filter`, `transaction-reconciliation`) |
| Planned arms | every gym-v1 task × evaluated agent(s) × k=3, purpose=baseline; plus one `oracle` control and one `nop` control per task |
| Registry blocker | **CLOSED** (4 task records approved by Peter Makhnatch with control evidence on 2026-08-19) |
| Lane status | Gemini Antigravity lane (`antigravity-cli`) proven via M037/M041; staged Low/Medium screen running |
| Billable execution | Zero billable trials dispatched without approval; queue inputs staged for review |
| Trials scored | **0** |
| Evidence rows | none (no comparative result claimed) |

## Result

**No result.** Zero trials ran, so there is no rate to report and no interval to
compute. No comparative result is claimed.

When trials exist, report rates with task-level evidence units and uncertainty
intervals; never as a bare percentage.

## Elicitation

Elicitation caveat: baseline configurations use standard harness defaults with no
extra preamble unless explicitly studying elicitation deltas (e.g. EXP-S03 via
`extra_instruction_path`), attempt count `k=3`.

Parameters recorded per trial: agent adapter, model pin, reasoning effort / thinking level,
preamble hash, toolset, attempts k.

## Contamination

Contamination caveat: **status recorded per task before capability claims.**
- `event-summary`: local synthetic scenario (low pretraining leakage risk).
- `query-optimize`: local Postgres optimization scenario.
- `terminal-bench-html-js-filter`: TerminalBench-derived task; public benchmark exposure risk exists.
- `transaction-reconciliation`: local financial reconciliation scenario.

Results from baseline waves support **behavioral** comparisons under specified harness
conditions; contamination disclosures must accompany external benchmarks.

## Threats to validity

1. **Pretraining exposure on imported benchmarks.** Tasks derived from public benchmarks
   carry pretraining exposure.
2. **Quota ceilings and window staleness.** Sizing waves must respect subscription windows
   and provider quota ceilings.
3. **Attempt floor (k=3).** k=3 yields coarse intervals; confidence intervals must be
   reported via Wilson/bootstrap intervals rather than point estimates.

## Regeneration query / command

```bash
# Verify the frozen manifest:
uv run python library/frozen/gym-v0/_freeze.py --generation gym-v1 --out /tmp/compare.json
diff <(jq -S . library/frozen/gym-v1/manifest.json) <(jq -S . /tmp/compare.json)

# Check registry status:
uv run python -m evallab.cli registry list
```

## Human review

Drafted following the closure of M032's empty-registry blocker and the promotion of
four registered task packages in `library/registry/`.
