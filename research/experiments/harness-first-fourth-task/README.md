# HAR-18 fourth-task admission packet

Factory packet so Integration/Peter can fill the HAR-14 **fourth slot** after registration. This does not register a task, does not freeze a fourth HAR-14 member, and does not run models.

## Selected cell

`dl-semantic-distractor-16384-s42` (16 KiB semantic distractor, seed 42). Matched twin: `dl-neutral-padding-16384-s42`. CI smoke cell `clean-baseline-4k` is **not** the cohort slot.

Details: [selected-cell.json](selected-cell.json), [rlm-fit.json](rlm-fit.json).

## Why not the weaker substitutes

Rejected with registry evidence in [substitutes.json](substitutes.json): `query-optimize`, `transaction-reconciliation`, `terminal-bench-html-js-filter`, `tau3-retail-1`. [loca-lean.json](loca-lean.json) rejects `loca-lean-v1` as an upstream-fetch duplicate construct.

## Exact gate

No materialized package exists under `derived/`. Production materialize requires `ACTION_MEMORY_WHEELHOUSE` and `ACTION_MEMORY_RESOLVER_PROVENANCE` ([wheelhouse-gate.json](wheelhouse-gate.json)). Factory did not stage wheels or run oracle/nop.

Peter-only registration checklist: [registration-checklist.json](registration-checklist.json). Factory must not write `library/registry/`.

## Index

[admission.json](admission.json)
