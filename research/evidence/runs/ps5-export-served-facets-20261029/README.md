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
  export ordinary run 0.523 -> 0.520 s. The old export query already narrowed the facet read with
  `a.trace_id IN (...)`. Cluster-scope L1-07 (165 members) 0.946 -> 1.080 s (+0.13 s); samples
  overlapped (old 0.74-1.12, new 0.82-1.30), so this delta is consistent with run-to-run noise,
  not a measurement of screening cost.
- Follow-up in-process measurement on L1-07, n=12: the complete facet screen had median CPU 0.00488 s
  (range 0.00477-0.00672 s), after admission and member selection. Across all 68 stored clusters,
  old and updated cluster-scope exports had identical facet fields for all 3,435 admitted rows.
  In separate fresh processes, first screen CPU medians were 0.02645 -> 0.02630 s
  (n=7 per version, alternating order); this cold measurement includes first-use costs.

Meaning: the facet-derived fields of both exporters now follow the same rule as `runs`: a withheld record
looks like no record. No sealing, admission or screening rule changed; the screen was extracted
(`unadmitted_seals`, `registry_seals`, `served_facet_rows`, `decode_row` in `cloud_trace_db.py`) and is shared.
