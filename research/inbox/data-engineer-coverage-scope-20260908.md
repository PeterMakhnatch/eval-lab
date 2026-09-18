# Scope-bound coverage product for Integration #389 (Data Engineer → Integration)

Product: `derived/coverage/coverage-scope-d9d0895d4c5b.json` (regenerable, git-ignored; rebuild byte-identical via `evallab.coverage_report.write_scope_bound_product`).

- Scope: **78 exact job IDs** — 11 finished Factory jobs (`factory-pr75-*`, `factory-facet-*`, `factory-compose-*`), 8 partial-intake jobs, tb21-codex-terra-slice + 2 oracle smoke jobs, ~56 cataloged Quality-build jobs without evidence on disk.
- Bound roots: source `/Users/petermakhnatch/Developer/eval-lab`, derived `/Users/petermakhnatch/Developer/eval-lab/derived/parquet`. Built only through existing APIs (`build_coverage_report` + scoped catalog loader + `verify_ingest`); no second store/schema, no copied runtime, no invented rewards/completion/ATIF.
- Outcome: projected 20, excepted 67, failed 1 (`factory-facet-semantic-oracle-20260908`, crashed execution on disk — real Factory signal, not a data gap). Reasons: evidence_absent 56, partial_intake 8, outside_checkout 2, job_unfinished 1. Agents: codex 4/4 trajectories, custom ZAI class 7/0, oracle 15 + nop 1 expected-absent.
- Binding proof embedded: same 78 IDs against a wrong root → 78/78 excluded (evidence_absent 76, outside 2, partial 8 preserved). Nothing borrowed from unrelated jobs/roots.
- External links (linked, not projected): Post-Training `capture-repair/qualification-output/manifest.json` (4 records: healthy + smoke eligible; invalid_logprobs + empty_abort rejected with reasons; CPU-synthetic origin explicit per record) and Quality `after-owned/000003 + 000011/comparison.json` (11 + 8 paired arms, direct-Docker origin explicit).
- Partial facts preserved: all 8 partial jobs list jobs+trial_facts only with `_partial.json` markers; zero reward/trajectory bytes.
- Missing facts preserved: 56 quality-build jobs are cataloged with no result.json on disk (evidence_absent); 2 factory-compose jobs point outside this checkout.

Consumer contract: read the JSON; `job_ids` is the closed world; `coverage.repair_path` gives resumable commands; `binding_proof` is the negative control. Regenerate any time — same inputs yield the same filename (sha in name).
