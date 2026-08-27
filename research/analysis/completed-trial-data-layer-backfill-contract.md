# Completed-Trial Data Layer Backfill & Operator Contract

- **Contract Status:** FROZEN / P0
- **Base Head:** `d68c1fb735b40a488014abc8c843393e87d2c215` (`origin/main` at freeze)
- **Authorizing Owner:** Peter Makhnatch
- **Execution Lane:** Ops Eval Runner (`wK:p8`)
- **Mission Controller:** Architect (`wK:p6`)

---

## 1. Canonical completed-trial pipeline

The completed-trial data layer has one ordered contract:

```text
Canonical TrialBundle (CAS raw archive / completed run tree)
  -> Completed-Trial Orchestrator (schema, identity, checksum and CAS validation)
  -> Event Node/Edge Projection (TrajectoryIR, multi-tool identity, non-causal edges)
  -> Data-Quality/Coverage Gate (coverage, StateJournal integrity, quarantine)
  -> PostgreSQL / Parquet / CAS (catalog, rebuildable projections, immutable bytes)
  -> EvidencePack (bounded selected/omitted partition and raw reopening)
  -> Disposition (ANALYSIS_READY or reason-coded HOLD; auto-accept disabled)
  -> Operator CLI / All-Durable Backfill
```

No stage may silently skip a completed trial, infer a missing identity, turn
unavailable into zero, or bypass a preceding quality gate.

### Target operator interfaces — not yet bound as one CLI command

The following names specify the target operator surface. `evallab data status`
and `evallab data backfill` are not implemented commands at this freeze and must
not be presented as executable until a reviewed CLI binding lands.

1. **Status target** (`evallab data status` or an exact reviewed replacement):
   - discover all completed trials across CAS, manifests, and catalog;
   - report PostgreSQL connected versus unconfigured;
   - report coverage, typed unknowns, quarantine isolation, and exact disposition.
2. **Backfill target** (`evallab data backfill` or an exact reviewed replacement):
   - run the canonical pipeline once for every durable completed trial;
   - persist catalog rows, Parquet/DuckDB projections, CAS-bound sidecars, and
     ANALYSIS_READY/HOLD records;
   - assert deterministic second-pass identity.
3. **Inspect target** (currently represented by reviewed analysis-inspect
   surfaces): reopen full artifact lineage, events, EvidencePack windows, and raw
   citations from exact CAS identities.

Existing reviewed commands may be composed internally during implementation,
but the milestone is not automatic until one operator entry point performs
discovery, processing, persistence, and exhaustive disposition reconciliation.

---

## 2. Canonical 21-trial durable inventory

### A. Five active interpretation campaigns

All 17 cohort trials are hold-only abstentions: 0 accepted, 0 rejected.

| Campaign ID | Manifest path | Trials | Report ID (display prefix) | Report CAS URI (display prefix) |
|---|---|---:|---|---|
| `terminal-bench-v3-k1-gemini-low-screen` | `research/experiments/manifests/terminal-bench-v3-k1-gemini-low-machine-analysis-inventory.json` | 5 | `sha256:0dac6649...` | `cas://sha256/d0bed3a8...` |
| `canary-event-summary-codex-20260815` | `research/experiments/manifests/canary-event-summary-codex-20260815-analysis-manifest.json` | 3 | `sha256:49e653ce...` | `cas://sha256/f24eaed5...` |
| `canary-terminal-bench-html-js-filter-codex-20260815` | `research/experiments/manifests/canary-terminal-bench-html-js-filter-codex-20260815-analysis-manifest.json` | 3 | `sha256:24060bfb...` | `cas://sha256/8be72ff7...` |
| `canary-transaction-reconciliation-codex-20260815` | `research/experiments/manifests/canary-transaction-reconciliation-codex-20260815-analysis-manifest.json` | 3 | `sha256:0048cf33...` | `cas://sha256/74958cfe...` |
| `canary-syn-funcdag-suite` | `research/experiments/manifests/canary-syn-funcdag-suite-analysis-manifest.json` | 3 | `sha256:732bcbb2...` | `cas://sha256/2a8ef312...` |

### B. Four quarantined trials

Each remains permanent HOLD, excluded from capability denominators and not sent
through interpretation:

1. `tb3-k1-bun-sourcemap-leak-gemini-low-quarantined`: pre-fix auth timeout;
2. `agentabstain-preview-002-act`: pre-fix Darwin verifier isolation failure;
3. `agentabstain-preview-002-abstain`: pre-fix Darwin verifier isolation failure;
4. `loca-bench/ab-testing-seed-42-8k`: pre-fix Darwin environment isolation failure.

### C. Sixteen archives backing twenty-one trials

- 4 single-trial quarantine bundles back the four quarantined trials;
- 5 single-trial TB3 screening bundles back the five TB3 cohort trials;
- 7 multi-trial canary/suite bundles back the remaining twelve cohort trials.

Therefore exactly `4 + 5 + 7 = 16` immutable job archives account for all 21
durable trial executions. The backfill must join by full job and trial identity;
archive count and trial count are different units.

**Normative identity invariant:** truncated IDs and digest prefixes in this
Markdown file are display labels only. Automated discovery, hydration,
persistence, and reconciliation MUST resolve complete UUIDs and full 64-hex
SHA-256 values from
`research/experiments/manifests/cross-campaign-analysis-inventory.json` and its
referenced manifests. Prefix matching is prohibited.

---

## 3. Data and claim invariants

1. TB3 is one arm, `k=1`, 0/5: Wilson 95% `[0.0, 0.4345]`; the
   all-zero bootstrap is suppressed as degenerate. No ranking, capability,
   reliability, or causal claim follows.
2. Missing/unrecorded verifier, state, token, cost, or identity fields remain
   typed unknown, never numerical zero.
3. EvidencePack is bounded below 16,000 tokens and every omitted member has an
   exact digest/reopening path; failure to reopen is HOLD.
4. Quarantine is preserved, CAS remains immutable authority, Parquet/DuckDB are
   rebuildable projections, and PostgreSQL is an identity/status catalog.
5. Judge/model calls and automatic acceptance remain disabled for this pass.
6. Model routing is Gemini 3.7 High, then Cursor Grok, then DeepSeek Flash;
   unavailability of all three is HOLD.

---

## 4. Execution gate

- **Current state:** interface frozen; execution blocked.
- **Merge order:** PR #223 canonical state-diff validation, then PR #218
  completed-trial evidence integrity/orchestrator integration.
- **Post-merge:** run one automatic all-durable backfill and reconcile every one
  of the 21 durable trials to ANALYSIS_READY or a reason-coded HOLD. No second
  pass is authorized except deterministic identity verification of that same
  backfill.
