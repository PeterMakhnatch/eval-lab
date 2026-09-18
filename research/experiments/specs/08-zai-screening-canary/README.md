# 08 — ZAI screening canary (17 cells)

Researcher brief §4 (`research/inbox/RESEARCHER-TOOL-USE-LOOP-BRIEF-2026-09-03.md`):
**17-trial screening canary** on the proxy lane. Every spec in this directory is
**screening / calibration-only**: n=1 per cell, not a ranking, not a comparison-bar
result, and **not submitted** — `queue/` is untouched. Filing date: 2026-09-03,
base `7a399d32`.

## Status: filed, pending the Stage-0 registration prerequisite

The brief sequences this canary after "materialize + register the 17 cells".
Registration has **not happened yet** (no `mcp-funcdag-*` / `recovery-*` records in
`library/registry/` as of filing), so every `task` ref below is a **planned
registered ref**, and `verifier_digest` / `task_path` are intentionally left
unset rather than guessed: `TaskRegistry.resolve_spec` binds both from the
registry record at admission (`src/evallab/registry.py:1894,1938`), and a
pre-filled wrong digest would raise `TaskDigestMismatchError`. Digests must come
from `library/registry/<task_id>.json` once records exist — never be invented.

Ref conventions:

- **FuncDAG (9)** — refs are the registry-registrable normalization of the
  materializer's deterministic IDs (`library/benchmarks/mcp-funcdag-v1/materializer.py:148`):
  registry pattern `^[a-z0-9][a-z0-9-]+$` forbids underscores, so
  `name_similarity_high` → `name-similarity-high`, `schema_drift_twin` →
  `schema-drift-twin`. Harbor/materialized task names keep underscores
  (`mcp-funcdag-name_similarity_high-seed42`, confirmed materialized under
  `derived/harbor-tasks/mcp-funcdag/`).
- **Recovery (8)** — refs follow the only in-repo recovery naming precedent
  (wave-1 `recovery-persistent-signature-error-s<seed>-clean`) with persistence
  encoded. Caveat: the certified materializer derives `mcp-rec-<hmac16>` IDs
  from per-pair secret keys (`mcp-recovery-v1/materializer.py:108`), so the
  final registered IDs are the registrar's choice — reconcile refs at
  submission, after materialization + registration.

## The 17 cells

All cells: agent `zai-opencode`, model `zai-coding-plan/glm-5.3-flash`, purpose
`baseline`, attempts 1, docker, timeout 1800s, priority 80, est_cost 0.0,
submitted_by `autopilot-researcher`.

### FuncDAG — `mcp-funcdag-v1` (`purpose=baseline`, twin-paired drift-vs-baseline only)

| # | Spec name | Planned task ref | generator_seed | Contrast role |
|---|---|---|---|---|
| 1 | `screening-funcdag-baseline-s42-zai-opencode-k1` | `registered/mcp-funcdag-baseline-seed42` | 42 | baseline arm of drift pair (s42) |
| 2 | `screening-funcdag-baseline-s101-zai-opencode-k1` | `registered/mcp-funcdag-baseline-seed101` | 101 | baseline arm of drift pair (s101) |
| 3 | `screening-funcdag-baseline-s2024-zai-opencode-k1` | `registered/mcp-funcdag-baseline-seed2024` | 2024 | baseline arm of drift pair (s2024) |
| 4 | `screening-funcdag-name-similarity-high-s42-zai-opencode-k1` | `registered/mcp-funcdag-name-similarity-high-seed42` | 42 | name-similarity factor arm |
| 5 | `screening-funcdag-name-similarity-high-s101-zai-opencode-k1` | `registered/mcp-funcdag-name-similarity-high-seed101` | 101 | name-similarity factor arm |
| 6 | `screening-funcdag-name-similarity-high-s2024-zai-opencode-k1` | `registered/mcp-funcdag-name-similarity-high-seed2024` | 2024 | name-similarity factor arm (pivot-trigger arm) |
| 7 | `screening-funcdag-schema-drift-twin-s42-zai-opencode-k1` | `registered/mcp-funcdag-schema-drift-twin-seed42` | 42 | drift twin of cell 1 |
| 8 | `screening-funcdag-schema-drift-twin-s101-zai-opencode-k1` | `registered/mcp-funcdag-schema-drift-twin-seed101` | 101 | drift twin of cell 2 |
| 9 | `screening-funcdag-schema-drift-twin-s2024-zai-opencode-k1` | `registered/mcp-funcdag-schema-drift-twin-seed2024` | 2024 | drift twin of cell 3 |

### Recovery — `mcp-recovery-v1` (fault+clean twins, persistence 1, seed 42, per-class PRR never pooled)

| # | Spec name | Planned task ref | FaultClass |
|---|---|---|---|
| 10 | `screening-recovery-transient-network-timeout-s42-p1-zai-opencode-k1` | `registered/recovery-transient-network-timeout-s42-p1` | `TRANSIENT_NETWORK_TIMEOUT` |
| 11 | `screening-recovery-transient-network-timeout-s42-p1-clean-zai-opencode-k1` | `registered/recovery-transient-network-timeout-s42-p1-clean` | clean twin |
| 12 | `screening-recovery-persistent-schema-mismatch-s42-p1-zai-opencode-k1` | `registered/recovery-persistent-schema-mismatch-s42-p1` | `PERSISTENT_SCHEMA_MISMATCH` |
| 13 | `screening-recovery-persistent-schema-mismatch-s42-p1-clean-zai-opencode-k1` | `registered/recovery-persistent-schema-mismatch-s42-p1-clean` | clean twin |
| 14 | `screening-recovery-silent-wrong-payload-s42-p1-zai-opencode-k1` | `registered/recovery-silent-wrong-payload-s42-p1` | `SILENT_WRONG_PAYLOAD` |
| 15 | `screening-recovery-silent-wrong-payload-s42-p1-clean-zai-opencode-k1` | `registered/recovery-silent-wrong-payload-s42-p1-clean` | clean twin |
| 16 | `screening-recovery-persistent-signature-error-s42-p1-zai-opencode-k1` | `registered/recovery-persistent-signature-error-s42-p1` | `PERSISTENT_SIGNATURE_ERROR` |
| 17 | `screening-recovery-persistent-signature-error-s42-p1-clean-zai-opencode-k1` | `registered/recovery-persistent-signature-error-s42-p1-clean` | clean twin |

(`malformed-output` / `TRANSIENT_HTTP_5XX` is deliberately excluded: the brief
names timeout/schema/silent/signature only — 4 classes × 2 twins = 8.)

## Kill rules (brief §4, verbatim decisions)

- **8/8 recovery pass → escalate difficulty.** All eight recovery cells
  rewarding 1.0 means the family is saturated at persistence 1; hold per-class
  PRR framing (never pooled) and escalate difficulty before drawing any
  recovery conclusion. Any recovery failure → keep difficulty, read per-class.
- **Name-similarity 3/3 → pivot to conflict synthesis.** If all three
  `name-similarity-high` seeds pass, the wrong-graph-traversal hypothesis is
  dead at this dose; pivot the Stage-2 synthesis target to the conflict cell
  (B2/cross-source, double-witnessed gap) instead of deepening name similarity.
- Drift is read only as the twin-paired drift-vs-baseline difference
  (cells 7–9 vs 1–3); `syn-funcdag-easy` stays banned from training per §5.

## Field notes (deviations from the `07-zai-opencode-baseline` mirror)

- `attempts: 1` — the canary is 17 trials, pass^1; the 07 file's k3 was a
  reliability probe, not this design.
- `generator_seed` added (42/101/2024 FuncDAG; 42 recovery = calibration-canary
  seed per `mcp-recovery-v1/README.md`).
- `task_family` added (`mcp-funcdag-v1` / `mcp-recovery-v1`) so analysis binding
  does not reproduce the `family_binding_absent` hold seen in the historical
  contract regeneration.
- `submitted_by: autopilot-researcher` — the researcher-flow actor stamp
  (`src/evallab/researchers.py:869`).
- `verifier_digest` / `task_path` omitted — registry-bound at admission; the
  source of truth is `library/registry/*.json` post-registration.
- `purpose: baseline` kept from the mirror per filing instruction; the
  screening/calibration-only qualifier lives in every `hypothesis`.
