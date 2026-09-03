# Canary17 registry-record DRAFTS (registrar input only)

Status: **DRAFTS ONLY — zero promotion.** Filed 2026-09-03 by `autopilot-researcher`
(night-shift batch 1) on `integrate/spine-batch1` @ `ccf5567e`. Nothing here is
registered: `library/registry/` was not touched, no queue submission, no approval,
no tick. Promotion to `library/registry/<task_id>.json` is a human-owned act
(`docs/task-registry.md`, `research/registration/REVIEW_PACKET.md` §5).

Inputs:

- Record template/schema: `library/registry/syn-funcdag-easy.json`
  (`schema_version` 2, `TaskRegistryRecord` in `src/evallab/schemas/__init__.py:2368`).
- Cell list + planned refs + HMAC-ID caveat:
  `research/experiments/specs/08-zai-screening-canary/README.md` (17 cells:
  9 FuncDAG + 8 recovery, brief §4 `research/inbox/RESEARCHER-TOOL-USE-LOOP-BRIEF-2026-09-03.md`).

## Layout

- `funcdag/*.json` — 9 draft records, **real digests** computed from the
  materialized Harbor packages under `derived/harbor-tasks/mcp-funcdag/`.
- `recovery/*.json` — 8 record **skeletons with explicit `TODO-NOT-MATERIALIZED`
  markers** (family `mcp-recovery-v1` has no materialized packages yet; nothing
  exists under `derived/harbor-tasks/mcp-recovery/`).

Both sets carry `state: "candidate"`, `state_reason: "control_evidence_pending"`,
`control_evidence: null`, `certification: {"state": "legacy_missing"}`,
`approved_by/approved_at: null` — i.e. deliberately non-registered.

## Digest methodology (FuncDAG — reproducible)

Digests were computed with the registry's own function, not by hand:

```
uv run python -c "from evallab.registry import compute_task_digests; ..."
```

`compute_task_digests` (`src/evallab/registry.py:145`) yields `task_toml`,
`instruction`, `environment`, `verifier`, `package` (package = aggregate over
sorted files, ignoring `.DS_Store/.git/__pycache__/.pytest_cache/.*.py[co]`).
Any re-materialization changes these bytes — recompute before promotion.

## FuncDAG index (9 records, digests bound to materialized packages)

Planned refs from the spec README, normalized to the registry id pattern
`^[a-z0-9][a-z0-9-]+$` (`name_similarity_high` → `name-similarity-high`).
`source_digest16` is the materializer's deterministic source digest == the
outer digest-addressed directory name (matches `task.toml` `metadata.source_digest`).

| Draft `task_id` | Materialized package | `source_digest16` | `digests.package` |
|---|---|---|---|
| `mcp-funcdag-baseline-seed42` | `derived/harbor-tasks/mcp-funcdag/b00232f71deb16bc/mcp-funcdag-baseline-seed42` | `b00232f71deb16bc` | `sha256:4148cd3b54f06fa2abe8b24ecf5c8fdba131e3aa9c6a401c9af212dda5c503d1` |
| `mcp-funcdag-baseline-seed101` | `derived/harbor-tasks/mcp-funcdag/5a00b986ac65f4a7/mcp-funcdag-baseline-seed101` | `5a00b986ac65f4a7` | `sha256:98d40c50b08e9a95cb6d6c0f501eadb8c9c9e4f7990b9365a7ba578b2677ed46` |
| `mcp-funcdag-baseline-seed2024` | `derived/harbor-tasks/mcp-funcdag/a09d041232723355/mcp-funcdag-baseline-seed2024` | `a09d041232723355` | `sha256:3fde1f90766b53b46349eccff7ab7aaf4163448d8d621bbf39af5fa5e939694a` |
| `mcp-funcdag-name-similarity-high-seed42` | `derived/harbor-tasks/mcp-funcdag/32d70893efd560fb/mcp-funcdag-name_similarity_high-seed42` | `32d70893efd560fb` | `sha256:ff015227dcfdd90a5d8fccc4dcdae0f012a5d599d45b3973382b4712a1b3b084` |
| `mcp-funcdag-name-similarity-high-seed101` | `derived/harbor-tasks/mcp-funcdag/9a8564b2cf58dc29/mcp-funcdag-name_similarity_high-seed101` | `9a8564b2cf58dc29` | `sha256:fdbef7876af56a3cb9a7a0a78c45582b0395caa7758ae5389aa7ad2f99bab340` |
| `mcp-funcdag-name-similarity-high-seed2024` | `derived/harbor-tasks/mcp-funcdag/d2735b6831d50c92/mcp-funcdag-name_similarity_high-seed2024` | `d2735b6831d50c92` | `sha256:75593139b6262e90b21f6bbd868bbe84890b289e5ae1da7a6f47031c4c033f60` |
| `mcp-funcdag-schema-drift-twin-seed42` | `derived/harbor-tasks/mcp-funcdag/72d270d48423e81b/mcp-funcdag-schema_drift_twin-seed42` | `72d270d48423e81b` | `sha256:67051ae119db334a31ca4ce41438a1915334f8d10d09d8a9dbc4e1b5dc55ff29` |
| `mcp-funcdag-schema-drift-twin-seed101` | `derived/harbor-tasks/mcp-funcdag/b08784fbf3bc4b90/mcp-funcdag-schema_drift_twin-seed101` | `b08784fbf3bc4b90` | `sha256:a500c0662a7e21fe4db39ccebb4a4b7cb16c4637452983418e49e7657253242a` |
| `mcp-funcdag-schema-drift-twin-seed2024` | `derived/harbor-tasks/mcp-funcdag/a52a69f380f85220/mcp-funcdag-schema_drift_twin-seed2024` | `a52a69f380f85220` | `sha256:e0a00438a927559cae7d9ca7919318498c1770ac53cd8e47c40882305cc6d49a` |

Shared field values (all 9): `task_family mcp-funcdag-v1`, `version 1.0.0`
(from each `task.toml`), `limits {timeout_seconds 180, max_memory_mb 512,
max_cpus 1.0}` (from each `task.toml` `[agent]`/`[environment]`), `license
Apache-2.0`, `provenance_zone 03-synthetic`, `is_synthetic true`,
`allowed_uses ["measurement"]`, `source_uri eval-lab/synthetic/mcp-funcdag-v1`,
`source_ref local/eval-lab/benchmarks/mcp-funcdag-v1@1.0.0`.

## Recovery skeletons (8 records) — reconciliation notes for the registrar

Skeletons: `recovery-transient-network-timeout-s42-p1{,-clean}.json`,
`recovery-persistent-schema-mismatch-s42-p1{,-clean}.json`,
`recovery-silent-wrong-payload-s42-p1{,-clean}.json`,
`recovery-persistent-signature-error-s42-p1{,-clean}.json`.
All eight carry `digests: "TODO-NOT-MATERIALIZED"` (all five components) and
`task_path: "TODO-NOT-MATERIALIZED"`. These markers intentionally FAIL
`TaskRegistryRecord` validation (`digests` pattern `^sha256:[0-9a-f]{64}$`),
so a skeleton cannot be promoted as-is.

1. **Final registered IDs are the registrar's choice.** Planned refs reuse the
   wave-1 `recovery-persistent-signature-error-s<seed>-clean` naming with
   persistence encoded (`-p1`), but the certified materializer derives
   `mcp-rec-<hmac16>` task IDs from per-pair secret evidence keys
   (`library/benchmarks/mcp-recovery-v1/materializer.py:108`,
   `_derive_task_id` = HMAC-SHA256(secret_key,
   `mcp-recovery-domain:<seed>:<fault>:<persistence>:<arm|clean>`)[:16]).
   Materialization output is digest-addressed:
   `derived/harbor-tasks/mcp-recovery/<digest16>/mcp-rec-<hmac16>/`.
   Reconcile the planned ref ↔ materialized `mcp-rec-<hmac16>` mapping **after**
   materialization + before naming `library/registry/<task_id>.json`; also
   update the spec table in `research/experiments/specs/08-zai-screening-canary/README.md`
   and the filed spec files' planned `task` refs if IDs change.
2. **Digests must never be invented.** Materialize first
   (`PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/materialize.py`;
   plan-only mode without `MCP_RECOVERY_WHEELHOUSE`), then compute
   `compute_task_digests` per package, then fill the five `digests` fields.
3. **Twin pairing is a contract.** Each fault cell must land with its matched
   clean twin (seed 42, persistence 1); per-class PRR is never pooled across
   classes in analysis.
4. **Calibration-canary seed.** Seed 42 is the calibration seed per
   `library/benchmarks/mcp-recovery-v1/README.md`; the 17-cell canary covers
   4 fault classes × 2 twins (the brief excludes `malformed-output` /
   `TRANSIENT_HTTP_5XX` deliberately).
5. **Lineage fields are provisional.** `source_uri`/`source_ref`
   (`local/eval-lab/benchmarks/mcp-recovery-v1@1.0.0`), `license Apache-2.0`
   (MIT Recovery-Bench is cited only as an ecological replay fallback, not
   vendored), and `allowed_uses ["measurement"]` mirror the FuncDAG drafts —
   the registrar should confirm before promotion.

## Promotion checklist (human registrar — per record)

1. Recompute all five digests from the on-disk package (FuncDAG: verify they
   match this draft; recovery: fill TODOs post-materialization).
2. Decide `task_path`. Drafts point at the materialized `derived/...` location
   where the digested bytes actually live; if registrar policy requires
   registered packages under `library/`, migrate first and recompute digests
   (a copy changes nothing, a rewrite changes everything).
3. Resolve the final `task_id` (recovery: `mcp-rec-<hmac16>` vs planned ref).
4. Run/promote oracle (1.0) + nop (0.0) control evidence, bind the workbench
   certification packet (`certification.state: "bound"`).
5. Human sets `state: "registered"`, `approved_by: "Peter Makhnatch"`,
   `approved_at: <UTC>`, clears `state_reason`, writes
   `library/registry/<task_id>.json`, commits via PR.
6. Only after registration: rebind the screening specs'
   `registered/...` task refs to the real registry IDs.
