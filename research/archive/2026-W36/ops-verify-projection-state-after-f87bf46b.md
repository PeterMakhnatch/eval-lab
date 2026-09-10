# Ops Eval Runner brief: verify projection reconciliation state after `f87bf46b`

## Assignment

Perform a strictly non-mutating, locator-authoritative reconciliation inventory and DuckDB attachment smoke against the exact final-approved data-plane head. Determine whether any projection backfill is authorized or necessary; do not execute a backfill or change repository/service state.

- Canonical worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch: `analyst/synth-data-promotion-hardening`
- Exact approved HEAD: `f87bf46b96868c7154293342f2860a2d54fe6468`
- Exact tree: `3461c4a75d174fc3017191da99917d40ae924f4e`
- Final approval: `/tmp/system-architect-final-authority-rereview-f87bf46b.md`
- Approved split: cryptographic CAS durable truth; manifest-gated PostgreSQL settlement; rebuildable Parquet projections; DuckDB query-only attachment.

Work read-only in the canonical worktree. Write no repository files, commits, manifests, projections, ledgers, CAS records, or database rows. Do not move/delete/quarantine files physically. Do not run models/controls. Do not spawn subagents. Temporary files outside the repository are allowed only for an isolated DuckDB session smoke and must not become evidence authority.

Deliver `/tmp/ops-verified-projection-state-reconciliation-f87bf46b.md` and page `wH:p9` with `VERIFIED`, `BLOCK`, or `AUTHORIZED-MISSING` plus exact counts and the report path.

## Authority rules

1. Treat a run/analysis as authoritative for reconciliation only when its exact terminal `EvidenceLocator` can be loaded and authenticated from an explicitly configured, already-existing CAS store. Never infer a locator from a raw path, job/trial name, historical contract, directory shape, environment fallback, or content similarity.
2. Require exact locator record kind/id/content digest, immutable materialization, exact settlement manifest source identity, per-table status/digest/count/schema, and ledger agreement according to the approved production APIs.
3. Historical regenerated contracts under `research/evidence/` are descriptive-only and are expected to lack CAS terminal locators. Classify them as `legacy-descriptive/non-eligible`, not missing backfill, and preserve ready/admissible `0/0`.
4. Unknown extra Parquet/manifests/ledger entries are inventory findings only. Classify them as `unknown-extra/hold`; do not move, delete, rewrite, attach as ready, or use them to infer authority.
5. A missing configured CAS store or PostgreSQL service is typed `unavailable`, not an empty success and not permission to use a default.

## Required inventory

Inspect the approved reconciliation/settlement/attach/data-backfill code and use its production read-only surfaces where available. Report a census with at least:

- candidate terminal locator records discovered from explicit authoritative event/manifest sources;
- locator-authenticated CAS records by kind;
- locator failures/missing blobs/digest mismatches;
- settlement manifests: ready, held, unavailable, invalid;
- projection tables: exact-ready, missing, stale/digest mismatch, schema mismatch, row-count mismatch;
- ledger rows consistent/inconsistent/missing;
- unknown extra projection/manifests/ledger entries;
- legacy descriptive historical contracts and why they are non-eligible;
- exact runs eligible for a deterministic projection rebuild;
- exact authorized missing rebuilds, if any;
- actual mutations: must be `0`.

For every eligible/missing item, include the complete locator and manifest identities needed for a future separately authorized rebuild. If none exist, state that zero backfill is the correct verified result rather than fabricating work.

## PostgreSQL boundary

- Use only an explicitly configured PostgreSQL DSN if one already exists.
- If absent/unreachable, exercise the production read-only preflight enough to capture the exact typed unavailable reason; do not create a database/schema/table or fall back to SQLite/local defaults.
- If present, use a read-only transaction/session and inventory only. Prove no writes, migrations, DDL, or settlement occurred.

## DuckDB attachment smoke

Exercise the actual production DuckDB attachment path against the inventoried settled projection root:

- create only an isolated temporary DuckDB session outside the repository;
- attach projections using production manifest/readiness gates;
- query actual attached table/view metadata and row counts for every table reported ready;
- prove held/missing/unknown-extra tables are not exposed as ready;
- report per-table readiness and query result;
- remove/discard the temporary session afterward.

If there are zero ready locator-backed projections, still exercise the production attachment boundary and prove it exposes no authoritative tables, with a typed reason rather than manufacturing fixtures.

## Preservation checks

Reconfirm without regeneration or edits:

- historical manifest SHA `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`;
- snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`;
- plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`;
- census `170/152/130/128/2/40/18`, ready/admissible `0/0`, 1,690 explicit-null design fields;
- worktree HEAD/tree and clean status unchanged before/after inventory.

## Verdict definitions

- `VERIFIED`: no authorized missing rebuild exists, or every locator-authoritative projection is already exact-ready; actual mutations `0`.
- `AUTHORIZED-MISSING`: one or more exact locator-authoritative rebuilds are deterministically authorized but missing/stale; provide complete identities and stop without mutation.
- `BLOCK`: reconciliation cannot establish authority because of digest/schema/ledger contradictions or an unsafe competing path. Missing external services alone are typed unavailable and may coexist with `VERIFIED` only when the remaining local inventory proves there is no authorized mutation to perform.
