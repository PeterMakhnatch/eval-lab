# Eval Lab current-state catch-up

**Snapshot basis:** approved staged spine `f87bf46b96868c7154293342f2860a2d54fe6468`; final Architect and Ops authority reports; and the root checkout/data audit stamped 2026-09-01 04:32 UTC.

## Executive answer

Eval Lab is between two phases:

1. **The new authority and settlement architecture is implemented, reviewed, and approved.** It gives the project a strong path from agent execution to immutable evidence, settlement, rebuildable projections, and guarded analysis.
2. **That approved spine is not on `main`, and the existing local corpus has not been reconciled into it.** The root checkout still exposes a legacy, partially inconsistent PostgreSQL/Parquet/CAS estate, while the isolated approved worktree correctly reports an empty active data plane.

The project is therefore not waiting for another architecture redesign or a larger benchmark catalog. The immediate job is to land the approved spine, migrate/reconcile the existing evidence into it, prove one complete end-to-end canary, then resume bounded evaluations.

## 1. The three states that currently coexist

| State | Observed status | Meaning |
|---|---|---|
| Approved staged spine | `analyst/synth-data-promotion-hardening @ f87bf46b`; clean; Architect `APPROVE`; 935 passed, 2 skipped | This is the current implementation authority. |
| `origin/main` | Local tracking head `93d2e7c1`; it is an ancestor of `f87bf46b`, but does not contain `f87bf46b` | The approved system is not deployed to the main spine yet. |
| Root working checkout | `feat/next-buildout-report @ 1848bfa0`, ahead 2/behind 1, with substantial user/untracked work | This contains the local legacy data audit and must not be treated as the approved implementation head. |

This distinction explains the apparently contradictory storage reports:

- the **root local estate** contains CAS records, PostgreSQL rows, and Parquet files;
- the **approved staged runtime environment** had no explicitly configured CAS root or PostgreSQL DSN, so it found zero locator-authoritative sources and correctly performed zero backfill.

Both observations are true. The missing step is a governed migration/reconciliation between them.

## 2. Current approved data pipeline

```text
agent/model + Harbor/runner
  -> exact queue attempt and terminal-event selection
  -> private read-only AnalysisWorker snapshot
  -> atomic canonical publication
  -> Z1 immutable CAS + exact EvidenceLocator
  -> Z2 PostgreSQL settlement ledger and active settlement manifest
  -> Z3 typed, rebuildable Parquet projections
  -> DuckDB manifest-gated query attachment and per-table readiness
  -> deterministic facts/features and evidence packs
  -> Analyst finding and/or bounded machine judgment
  -> deterministic admissibility/acceptance gates
  -> operator marts, reports, and dashboards
```

### Durable authority versus derived data

| Layer | Role | Current approved contract |
|---|---|---|
| Canonical run publication | Durable trial bundle and exact job/spec/attempt identity | Lexical path restriction, ancestor no-follow checks, UUID/provenance binding, native atomic no-replace publication |
| Z1 CAS | Raw immutable evidence | SHA-256 content/archive identity; explicit `EvidenceLocator`; no implicit store fallback |
| Z2 PostgreSQL | Operational catalog and settlement authority | Append-only settlement transitions; typed unavailable state if `DATABASE_URL` is absent |
| Settlement manifest | Published generation/table admission | Binds source locator, schemas, files, counts, and readiness; DuckDB cannot bypass it |
| Z3 Parquet | Rebuildable analytical projections | Typed Arrow/Parquet tables, atomic file publication, source/manifest verification |
| DuckDB | Query-only unified surface | Attaches Z2/Z3/Z4; exposes per-table readiness; missing authority remains missing rather than becoming empty evidence |
| AnalysisWorker | Guarded analysis execution | Frozen request, private `0o400`/`0o500` tree, pre/post digest checks, invalid sidecar quarantine, no false admissibility minting |

The approved attach registry currently describes 39 logical Z3 relations, including trial/reward facts, trajectories, steps, tool calls, semantic facts, interpretation artifacts, judgments, decisions, and Inspect relations.

## 3. What data actually exists locally

The root checkout's read-only audit observed the following legacy estate:

| Store | Observed local inventory | Important qualification |
|---|---:|---|
| Completed raw job directories | 78 directories / 66 distinct Harbor job IDs | 12 duplicate directories; not a complete match to the catalog |
| CAS | 212 records, all record/blob links and archive digests verified | Strong objects, but not every catalog job has a directly matched raw/CAS identity |
| PostgreSQL | 94 experiments, 156 jobs, 189 trials, 446 rewards, 506 artifacts, 3,177 run files | 102/156 stored job evidence paths and 103/189 trial paths were missing on disk |
| ATIF/deterministic indexes | 72 trajectory documents; 189 deterministic trial facts | Useful mechanical coverage, but not complete unified projection coverage |
| Parquet | 2,543 files, 10.77 MB total, approximately 4.2 KB/file | A small-file lake with overlapping hot/cold layouts |
| Legacy DuckDB attach | 27 of 39 declared Z3 tables visible | This is the old path-based attach, not the new manifest-admitted plane |
| Interpretation plane | 195 interpretation artifacts; 39 machine judgments; 39 acceptance decisions | All 39 judgments were deterministic abstentions; automatic acceptance was disabled |
| Analyst plane | 0 invocations, 0 findings, 0 citations, 0 reviews | The reviewable Analyst system exists in code but is not populated |

### Legacy completeness problems

The legacy projection invariant was:

```text
catalog jobs = 156
complete projected jobs = 50
recorded exceptions = 0
missing = 106
extra projected IDs = 10
```

Other important observed gaps:

- only 56/156 catalog job IDs were present in the two primary raw roots;
- another 12 had a direct CAS match, leaving 88 without a discovered raw job or direct CAS match in the inspected sources;
- `traj_features` had 419 rows but only 240 distinct `(job_id, trial_id)` keys, with 179 excess rows;
- interpretation Parquet existed outside the legacy attach root;
- four backup dumps passed digest checks, but no automated restore drill existed.

Several code-level causes in that audit were subsequently repaired on `f87bf46b`: exact CAS settlement, generation/manifest gating, interpretation projection-root unification, per-table typed readiness, and cross-plane reconciliation. The data itself was not migrated, so the old counts still require reconciliation rather than dismissal.

### Approved-plane operational population

In the isolated approved worktree, Ops observed:

- no explicit `EVALLAB_EVIDENCE_STORE_ROOT`;
- no `DATABASE_URL`;
- zero caller-supplied terminal locators;
- zero locator-authenticated sources;
- zero active settlement manifests;
- zero ready projection tables;
- all 39 readiness rows reported `missing: no active settlement contract`;
- zero mutations and zero authorized missing rebuilds.

That is correct fail-closed behavior, not data loss. It proves the approved plane does not infer authority from legacy directories.

## 4. Analysis and feature capabilities

### Implemented analysis machinery

- ATIF normalization and validation across the supported ATIF revisions, including steps, tool calls, observations, nested paths, and trajectory identities.
- Deterministic mechanical facts: trial/reward facts, action/effect/state events, tool usage, error classifications, trajectory phases, quality findings, and baseline dynamics.
- A typed trajectory IR with citations, coverage accounting, episode segmentation, evidence packs, and fail-closed budget/quarantine behavior.
- Outcome authority and regrade lineage using a vector rather than a single ambiguous status: `(agent, verifier, artifact, authority, admissibility)`.
- Statistical helpers for Wilson intervals, Fisher exact tests, paired binary contrasts, sequence fidelity, repeat heterogeneity/ICC, and design effects.
- Counterfactual trajectory alignment for divergence/reconvergence analysis.
- An isolated Analyst/AnalysisWorker system capable of producing reviewable findings and bounded sidecars from frozen evidence.

### Feature inventory

| Inventory | Count/state | Interpretation |
|---|---:|---|
| Autonomous-research features | 74 | Covers loop dynamics, score/budget curves, selection regret, anytime/final behavior, and contamination/provenance flags |
| Registered trajectory features | 240 | Broad deterministic, screening, benchmark-specific, and outcome-linked registry |
| Predictor-eligible | 109 | Eligible under the current registry audit |
| Predictor-refused | 131 | Refused for coupling, timing, leakage, or other governance reasons |
| Missing temporal declarations | 72 | Metadata debt; cannot be bulk-filled from names |
| Missing denominator applicability | 73 | Important for opportunity-conditioned rates |
| Undeclared coupling refusals | 3 | Must be resolved before predictive use |

The feature definitions are ahead of the data plane. In the legacy lake, many semantic tables are empty, the feature table contains duplicate logical identities, and no canonical activation/usefulness view exists. Feature counts should not be interpreted as populated or validated evidence.

### Model analysis is not yet operating as a loop

- The root database has zero rows in all four `analysis_*` tables.
- The 39 persisted “machine judgments” are deterministic abstentions for insufficient evidence, not successful model-judge analyses.
- The approved staged plane has no active locator-backed input to analyze.
- Some SQL/view definitions exist, but there is no reliable, generation-bound operator mart covering projection completeness, feature activation, outcome authority, Analyst findings, and judgments together.

## 5. Evaluations and benchmark families

Eval Lab's intended portfolio is deliberately limited to three research questions rather than a benchmark zoo.

### Theme 1 — autonomous research and improvement

| Evaluation | Current status |
|---|---|
| RSI-Exam | Active anchor. BBO and Game2048 calibration evidence exists; Game2048's verifier regrade is `0.37800819`. Both remain calibration-only because the Darwin task copy used public egress/reduced timeouts. |
| RE-Bench | Support benchmark; import/limited execution after task audit. |
| MLE-bench | Support/import-first; high environment cost. |
| CORE-Bench | Limited support for reproducibility and dependency repair. |
| PaperBench | Metric/rubric precedent, not a priority full suite. |

### Theme 2 — stateful tool use and recovery

| Evaluation | Current status |
|---|---|
| MCP Recovery / tool composition / FuncDAG | Producers and historical Z.ai/OpenCode calibration reports exist. The proposed registered campaign at `7f70abd1` remains design-only and must not run. It still needs a secured key+wheelhouse receipt, exact registered revisions, generic reservation/attempt identities, a qualified profile, and Linux isolation/admissibility. |
| ToolSandbox | Preferred external anchor, but task/harness and verifier work remain. |
| τ²-bench | Adjacent support; add only after internal recovery analyses work. |
| ToolMaze | Watchlist/metric candidate; method-level audit still required. |

### Theme 3 — memory, context and continuity

| Evaluation | Current status |
|---|---|
| Action Memory | Historical Phase-A/E0b calibration reports and strong producer/control code exist. The current deterministic control stack is fixture-only; it lacks a registered package, regular verifier execution, CAS settlement, and outcome-authority control evidence. |
| LoCoMo Conv-26 | Source/candidate evidence exists, but there is no distinct deterministic fair-alternative solver under the no-model rule. Runtime hardening, transformation evidence, certification, registration, and canary execution remain prohibited. |
| MemGym | Strict source-only, zero-compaction ingestion is integrated. Compaction, corpus, certification, registration, measurement, and activation remain on HOLD. |
| MemoryAgentBench / LOCA-bench | Support/watchlist candidates after the core write/read/use producer is proven on real governed data. |

Historical Z.ai/OpenCode reports include the MCP pilot, MCP wave 2, Action Memory Phase A, and E0b handle-representation runs. They are useful descriptive calibration evidence, not causal-grade or leaderboard evidence. The strict historical regeneration preserves 130 descriptive contracts and deliberately marks zero as analysis-ready or admissible.

## 6. Model runtime readiness

| Lane | Current observed verdict |
|---|---|
| `oracle`, `nop` | Ready / canary-qualified |
| Codex | Ready |
| Antigravity / Gemini 3.7 Flash High | Ready; corrected event-summary smoke passed with reward 1.0 and ATIF capture |
| Other Antigravity pins | Credential-ready; each exact pin still needs its own smoke |
| Cursor CLI | Host-ready, Harbor-container blocked because the installed adapter requires `CURSOR_API_KEY` |
| Claude Code | Blocked by missing credential/keychain item |
| DeepSeek mini-swe-agent | Blocked by missing provider keys |

The intended readiness contract is provider-neutral: declared, installed, host credential, container credential transport, model compatibility, environment compatibility, trajectory completeness, smoke, and repeated canary qualification.

## 7. What is still missing

### Integration and deployment

1. `f87bf46b` is not on `main`.
2. No authoritative CAS root/DSN/derived root has been designated for the approved plane.
3. Legacy raw/CAS/catalog/projection identities have not been mapped into exact locators and settlement generations.

### Data truth

1. The 156-job catalog is not reconciled to raw/CAS authority.
2. The old Parquet lake has 106 missing and 10 extra projection IDs under its legacy invariant.
3. Feature rows have duplicate logical identities.
4. Backup integrity is known, but restore readiness is not.

### Analysis operations

1. No populated Analyst plane.
2. No successful authorized model-judgment loop.
3. No stable attached mart presenting completeness, outcome authority, feature activation, and analyst/judgment state together.
4. Feature temporal/denominator/coupling metadata is incomplete.

### Evaluation activation

1. RSI evidence is calibration-only.
2. MCP/FuncDAG's new campaign is design-only.
3. Action Memory controls are fixture-only.
4. LoCoMo is blocked at the deterministic solver/certification boundary.
5. MemGym is source-only.
6. Causal-grade claims still need Linux-enforced isolation; Darwin public-egress runs remain calibration-only.

### Throughput, after truth is restored

The legacy audit identified repeated trajectory decoding, 2,543 tiny Parquet files, whole-archive citation hydration, manual marts, and incomplete compaction coverage. These are real efficiency targets, but should follow identity/reconciliation work rather than precede it.

## 8. Recommended sequence

### Priority 0 — land the approved spine

Fast-forward or merge `analyst/synth-data-promotion-hardening @ f87bf46b` onto the current main spine after the normal current-remote check. Do not continue platform work from the dirty root branch as if it were the approved implementation.

### Priority 1 — reconcile the existing estate into the approved plane

1. Explicitly designate the authoritative CAS root, PostgreSQL DSN, and derived root.
2. Map every catalog job to an exact verified `EvidenceLocator` or an explicit reason-coded, owned exception.
3. Investigate the 88 legacy catalog jobs without a discovered raw/direct-CAS match; do not silently remove them from the denominator.
4. Settle and rebuild projections through the new manifest path.
5. Require all 39 tables to be `ready`, deliberately `optional-missing`, or held with an explicit reason.
6. Reproject/deduplicate `traj_features` at a declared versioned primary-key grain.
7. Run an isolated PostgreSQL restore drill and record it.

### Priority 2 — prove one end-to-end no-cost canary

Run a fresh registered deterministic `oracle`/`nop` or equivalent canary through the actual queue:

```text
execute -> canonical publish -> CAS locator -> PostgreSQL settlement
        -> active manifest -> Parquet -> DuckDB -> analysis/admissibility
```

Acceptance should include: exact terminal locator, active settlement manifest, queryable trial/reward/trajectory rows, zero unexplained reconciliation gaps, and no authority minted from invalid/absent sidecars.

### Priority 3 — build the operator truth surface

Materialize stable generation-bound views for:

- projection/source completeness;
- agent readiness;
- composite outcome validity and reward authority;
- headline/scale/selection binding;
- feature activation and predictor eligibility;
- Analyst findings, judgments, acceptance, and their source digests.

Some component views already exist in source; the requirement is one attached, versioned, population-aware surface rather than manual session SQL.

### Priority 4 — resume one bounded real evaluation per useful theme

1. **Theme 1 first:** one clean-completion RSI slice using the already ready Gemini 3.7 Flash High and Codex lanes. Keep Darwin results calibration-only unless moved to Linux isolation.
2. **Theme 3 next, fastest controlled path:** package/register Action Memory controls and run the regular verifier through CAS/outcome authority. This reuses the most existing machinery and is lower methodological risk than pretending LoCoMo is ready.
3. **Theme 3 external-validity path:** implement and independently validate the distinct LoCoMo fair-alternative solver, then harden/certify/register the final package before any canary.
4. **Theme 2:** compile the MCP/FuncDAG campaign only after its exact registration, materialization receipt, generic identity, profile, and Linux prerequisites are satisfied.

Do not add another benchmark family until at least one cheap corpus produces queryable, governed analysis through the new plane.

### Priority 5 — reduce and optimize from evidence

After a real governed corpus exists:

- complete the 72 temporal and 73 denominator declarations plus three coupling decisions;
- publish a feature usefulness ledger and prune/refine dormant features;
- decode each trajectory once into one projection bundle;
- batch PostgreSQL/Parquet writes;
- make compaction generation-atomic;
- cache CAS hydration once per archive per analysis operation.

## Bottom line

Eval Lab has crossed the architecture-quality threshold, but not the operational-closure threshold. The correct next milestone is not “more features” or “more evals.” It is:

```text
approved spine on main
  + legacy evidence reconciled into exact locators/manifests
  + one complete canary visible through DuckDB and analysis
  = trustworthy platform ready for new measurements
```

After that, the best immediate research sequence is a clean RSI run, governed Action Memory controls, then LoCoMo or MCP/FuncDAG once their explicit certification/isolation prerequisites are satisfied.

## Primary evidence

- `/tmp/system-architect-final-authority-rereview-f87bf46b.md`
- `/tmp/ops-verified-projection-state-reconciliation-f87bf46b.md`
- `research/inbox/program-status-final-f87bf46b.md`
- `research/inbox/repo-custodian-program-integration-order.md`
- `research/analysis/data-pipeline-storage-state-report.md`
- `research/analysis/agent-runtime-readiness-2026-08-31.md`
- `research/analysis/eval-lab-next-buildout-report-2026-08-31.md`
- `research/analysis/thematic-benchmark-portfolio.md`
