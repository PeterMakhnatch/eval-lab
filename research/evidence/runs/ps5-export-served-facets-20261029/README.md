# ps5 export served facets (2026-10-29)

Question: after `QaContext.served_facet_run_ids` (PR #1540) made `traces`/`task`/`trace-card` call a run
faceted only when `runs` serves its facet record, do `qa-data export` and `prod export-cluster-scope`
still show facet-derived fields for a run whose record `runs` withholds (sealed-name substring match)?

Number (store copy `/private/tmp/ps2/store.sqlite`, real CLI, old = origin/main `5a1a7bd4a`, new = this branch;
`measure.py`, raw numbers in `results.json`):

- `export --trace-id retained-trace:b493cd8d3a512fe3bd44d165bfb0f6a4` (admitted task
  `terminal-bench/layout-config-recreation2`, facet withheld): old `evidence_steps_original=[0,1]`,
  `evidence_steps_blinded=[1,2]`; new both `[]`. Every other reply field is identical.
- Ordinary faceted run `retained-trace:227bec15e5cf1617b2d3f56225f8e1a3`: reply identical (`[2]`/`[3]`).
- `export-cluster-scope` for L1-02, L1-02.2 and L1-02.2.1 (the clusters holding the withheld run): same members,
  same counts, same rows; the only changed row is the withheld run, `facet_producer_revision`/`facet_served_model`
  `run-facets/v1` / `xai-oauth/grok-4.20-0309-non-reasoning` -> null / null. L1-07 (no withheld run): identical.
- CPU (user+sys of the CLI process, n=7, old/new interleaved, medians): export withheld run 0.587 -> 0.538 s,
  export ordinary run 0.523 -> 0.520 s (facet read is now narrowed to the exported run ids);
  cluster-scope L1-07 (165 members) 0.946 -> 1.080 s (+0.13 s): that is the screening cost
  (seal set over every non-admitted task plus whole-record sealed-name search), the same screen `runs` pays.
  Samples overlapped (old 0.74-1.12, new 0.82-1.30), so treat the scope delta as approximate.

Meaning: the facet-derived fields of both exporters now follow the same rule as `runs`: a withheld record
looks like no record. No sealing, admission or screening rule changed; the screen was extracted
(`unadmitted_seals`, `registry_seals`, `served_facet_rows`, `decode_row` in `cloud_trace_db.py`) and is shared.
