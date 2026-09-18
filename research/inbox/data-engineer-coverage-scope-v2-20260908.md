# Scope-bound coverage v2: R2 singleton + corrected 79-set (Data Engineer → Integration)

Supersedes `data-engineer-coverage-scope-20260908.md` for R2 purposes; v1 product
`derived/coverage/coverage-scope-d9d0895d4c5b.json` (78 IDs, pre-R2) is preserved
unchanged. Two wrong-UUID intermediate drafts (never delivered, never referenced)
were removed to prevent consumer confusion; their sha names are recorded in the
lane handoff, not on disk.

## Singleton R2 product (what #389 asked for)

`derived/coverage/coverage-scope-90e3c9e9a058.json` — `job_ids` is exactly
`["f94f1507-7958-4e08-addb-92b50d97c387"]` (the evidence UUID from R2's own
`result.json`, matching Integration's request — NOT the transposed `4c80`
variant that briefly appeared as a catalog row).

- Roots: source `/Users/petermakhnatch/Developer/eval-lab`, derived `.../derived/parquet`. Same roots as requested.
- Coverage: catalogued 1, projected 1, native 1; oracle 1 trial with trajectory expected-absent; reasons {} (nothing missing); repair [] (nothing to fix). Wrong-root negative embedded (excluded 1, evidence_absent).
- Spec binding in `external_links`: spec `01M2199Y5ZE41QNPVCS7TSKA3Y`, done spec `queue/done/oracle-01M2199Y5ZE41QNPVCS7TSKA3Y.json`, policy `local-controls`, full queue chain with timestamps.
- Regenerable byte-identical via `evallab.coverage_report.write_scope_bound_product`
  (determinism pinned by test).

## Corrected 79-set: `derived/coverage/coverage-scope-3454aaa747d7.json`

Same 79 world as before but with the evidence UUID: catalogued 21/21 names,
projected 23, native 21 (scoped — no longer the root-wide 103), agents real
(codex 4/4, custom ZAI 7/0, oracle+nop expected-absent), reasons scoped
(evidence_absent 56, partial 8, outside 2, unfinished 1; no borrowed global
exception counts), failed 1 genuine (`factory-facet-semantic-oracle-20260908`,
on-disk crash — Factory's signal). Wrong-root proof 79/79 excluded.

## Root causes behind the three reported ambiguities

1. **78 vs 20 names, truncated=false**: `catalogued.count` came from the unfiltered
   verification total while `jobs` listed only retained names. Fixed: count and
   names describe the retained set; excluded jobs live under `excepted` with
   reasons. Pinned by `test_section_counts_agree_with_listed_names` (every
   section: untruncated ⇒ count == len(jobs)).
2. **native 103**: disk scan was root-wide in a closed-world product. Now scoped
   to selected evidence dirs when `selected_job_ids` is set; default path unchanged.
3. **R2 identity**: evidence UUID `...4e08` (stable since dispatch 15:53 EDT,
   result.json mtime-verified). A catalog row with transposed UUID `...4c80` was
   observed, then both that row and the 19:42 partition vanished by 19:44 with no
   queue event during concurrent lane ingests. Evidence stayed intact throughout.
   No re-ingest was performed (explicit instruction + live race risk); the row +
   partition have since reappeared under the evidence UUID and the singleton above
   verifies against that live state.

Consumer contract unchanged: `job_ids` is the closed world; `repair_path` commands
are informational; `binding_proof` is the negative control; sha in filename is the
version identity — never edit a product file, supersede it.
