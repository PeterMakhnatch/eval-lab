---
source_type: internal
---

# PR fix briefs — 2026-09-02 review sweep

Read-only reviews of every open PR were run today; merged: #348, #230, #345, #349 (pending CI), #352 (pending CI). Closed with reasons: #344, #347. The four below need owner action in their own worktree; do not edit the primary checkout.

## #350 `[codex] Add secure LLM request projection` — owner: Platform Builder (wH:p1)

Blockers (CI red: lint, test 3.12, test 3.14):
1. 17 test failures per matrix from new non-null Parquet fields without fixture defaults: `tests/test_parquet_compaction.py` (11 tests, `steps.llm_metadata_available` omitted → ArrowInvalid), `tests/test_compaction_properties.py` (3 tests, same plus `llm_calls.metadata_available`), `tests/test_trajectory_queries.py::test_all_trajectory_intelligence_queries_execute` (missing defaults for new `list<string>` fields).
2. `tests/test_repository_contract.py::test_repository_has_no_high_confidence_secrets` fails: `tests/test_llm_request_projection.py:151` commits an `sk-` literal. Build the token dynamically as `test_secret_scanner_detects_standalone_api_key_shape` does.
3. Lint job fails on repomap freshness: regenerate `docs/repo-map.md` (`uv run python -m evallab.repomap generate`).
4. Design gap: `src/evallab/evidence/atif.py:718-727` only invokes `project_llm_requests` when ATIF has zero tool calls and zero `llm_call_count`. A partial-but-nonempty ATIF (the Goose case) is never supplemented. Use an explicit completeness decision instead.
5. Historical Parquet partitions lack the new non-null columns; state the defaulting/migration rule before compaction touches old rows.

Overlaps: #346 and #352 both touch `src/evallab/evidence/atif.py`; rebase onto main after #352 lands.

## #351 `fix: bind Tau and BFCL canary provenance` — owner: Platform Builder (wH:p1)

Base was retargeted from `feat/next-buildout-report` (already squash-merged as #343) to `main`; lint/test/ty will now run.

Blocker: `scripts/tau_knowledge/materialize.py::_generate_docker_compose` injects any `TAU3_SIMULATOR_BASE_URL` into the sidecar as `OPENAI_BASE_URL`, but neither `harden_sidecar_environment` nor `materialize` calls `credential_preflight`, and `scripts/tau_knowledge/preflight.py` has no execution caller. A materialized task can therefore still point at `http://localhost:11434/v1` despite preflight rejecting it. Put the URL gate on the materialization path itself.

Note: `scripts/bfcl_preflight.py` returns `status=proceed` while reporting `registry_status=blocked_stale_legacy_pin`; `library/benchmarks/bfcl-parity/source-lock.json` dataset/adapter revisions are declarative until a launch boundary consumes them. Acceptable for the one authorized exploratory BFCL run, but say so in the PR body.

## #346 `feat: map canonical ATIF tool calls onto memory-continuity facts` — owner: Engineer - Agent Data (wK:p9)

Blockers:
1. Targets `data/locomo-atif-ingest-onto-6eebed87`, not `main`, so the 14k-line diff includes the unmerged parent stack. Land or squash the parent, then retarget to `main`.
2. `certify-easy` fails (run 33469117354): `src/evallab/trial_admissibility.py` resolves both `artifacts/app/output/result.json` and `result.json` as mutually exclusive outcome sources → `trial_admissibility_invalid:ambiguous-outcome-sources`. Fix and rerun certification.
3. No LoCoMo ATIF, task, or provenance is committed (`research/inbox/agent-data-locomo-feature-ingestion-reply.md` says "no real LoCoMo ATIF observed"); the mapper has only synthetic unit coverage. Do not claim LoCoMo ingestion in the PR body.

## #272 `Orient agents and demote living-doc inventories` — owner: Repo Custodian (wH:p0)

Conflicts are confined to generated `docs/INDEX.md` and `docs/repo-map.md`. Rebase onto current main, regenerate both, and refresh `docs/NOW.md`: its 2026-08-28 lane list tells agents to avoid #261/#262/#263/#267/#268 (all merged) and calls #260 live (closed). Merge the docs-truth cutover delivered at `071579c3` into the same rebase so `docs/NOW.md` lands once as the sole authority.
