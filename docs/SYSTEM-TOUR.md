---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# System Tour: End-to-End Evaluation Architecture

Eval Lab is an evaluation workbench built around Harbor that enforces immutable evidence, verifiable provenance, and guarded execution feedback loops.

---

## 1. Canonical 7-Stage End-to-End Pipeline

```text
[1. TASK & EXPERIMENT SPEC]
       │  • Task Package: library/tasks/<name> (TaskRegistryRecord in evallab.schemas)
       │  • Experiment Matrix: research/experiments/<name>.json (ExperimentSpec)
       │  • Difficulty Screening: evallab ladder screen stage1 -> stage2
       ▼
[2. ADMISSION & CONTROL PLANE]
       │  • Preflight Quotas: evallab preflight
       │  • Queue Submission: evallab submit <spec.json> (queue/pending/)
       │  • Approval Gate: evallab approve <spec_id> --actor peter
       ▼
[3. HARBOR EXECUTION & VERIFICATION]
       │  • Reconcile & Dispatch: evallab tick (runs via Harbor container sandbox)
       │  • Direct Local Controls: evallab run --task library/tasks/... --agent oracle|nop
       ▼
[4. RAW EVIDENCE IMMUTABILITY (Zone 1 & CAS)]
       │  • Raw Trial Directory: runs/trial_jobs/<job_id>/<trial_id>/ (result.json, ATIF, logs)
       │  • Content-Addressed Storage: derived/evidence-cas/ (cas://sha256/<hex>)
       │  • Promoted Golden Evidence: research/evidence/runs/ (Permanent retention)
       ▼
[5. METADATA CATALOG & DATA LAYER INGESTION (Zones 2 & 3)]
       │  • PostgreSQL Catalog (Z2): evallab ingest runs -> sql/schema.sql (jobs, trials, rewards, verdicts)
       │  • Parquet Analytics Lake (Z3): derived/parquet/ (evallab.evidence.facts, evallab.storage.parquet_compaction)
       │  • Reconciliation / Backfill: evallab data backfill --all (disposition: ANALYSIS_READY vs HOLD)
       │  • Unified Query Surface: evallab db attach --zones (DuckDB across Z2+Z3+Z4)
       ▼
[6. TRAJECTORY INTERPRETATION & EVALUATION (Zone 4)]
       │  • IR Extraction: evallab traj ir <trial> (TrajectoryIR in evallab.interpretation)
       │  • Evidence Pack: evallab traj pack <trial> (Bounded citation EvidencePack)
       │  • Machine Judgment & Acceptance: evallab.interpretation (MachineJudgment -> AcceptanceDecision)
       │  • Capability Curves & Eval Cards: evallab curve build <spec> / evallab card generate <spec_id>
       ▼
[7. GOVERNED FEEDBACK & HUMAN VERDICTS]
       │  • Append-Only Verdicts: evallab verdict record <discovery_id> --status ACCEPTED --by peter
       │  • Synthesis & Daily Digest: evallab digest / evallab status --update
```

---

## 2. Authoritative Package Boundaries

The code layout is strictly modularized across authoritative domain packages and top-level entrypoints:

- `src/evallab/schemas/`: Pydantic v2 domain schemas, contract models, and join spine invariants.
- `src/evallab/storage/`: Filesystem paths (`paths.py`), DuckDB unified attach (`attach.py`), Parquet compaction (`parquet_compaction.py`), and historical backfill (`data_backfill.py`).
- `src/evallab/evidence/`: Canonical ATIF normalization (`atif.py`), fact extraction (`facts.py`), and event marts (`event_mart.py`).
- `src/evallab/interpretation/`: Canonical trajectory intermediate representation (`trajectory_ir.py`), bounded model context (`evidence_pack.py`), machine evaluation (`trajectory_judgment.py`), quality screening (`trajectory_quality.py`), and platform governance gates (`trajectory_acceptance.py`).
- `src/evallab/recovery/`: State recovery certification (`certify.py`), bundles (`bundle.py`), and paired pilots (`pilot.py`, `wrapper.py`).
- Top-level modules (`src/evallab/*.py`):
  - Execution runners & queues: `runner.py`, `queue.py`, `preflight.py`, `quota.py`
  - Task registries & authoring: `registry.py`, `ladder.py`, `screen.py`, `task_workbench.py`, `authoring.py`
  - Synthetic generation: `seqgen.py`, `synthetic_funcdag.py`, `synthetic_transform.py`, `synthetic_cert.py`
  - CLI & reporting: `cli.py`, `status.py`, `repomap.py`, `docindex.py`, `verdicts.py`

---

## 3. Four Provenance Zones & Storage Guarantees

1. **Zone 1: Raw Durable Evidence & External Benchmarks**
   - Locations: `runs/trial_jobs/`, `research/evidence/runs/`, `derived/evidence-cas/`, `library/benchmarks/_trajectories/`
   - Policy: Immutable, append-only, and protected from garbage collection.
2. **Zone 2: Relational Metadata Catalog**
   - Locations: PostgreSQL database (`jobs`, `trials`, `verdicts`, `rewards`)
   - Policy: Fast operational queries and indexing; never stores heavy blob data.
3. **Zone 3: Columnar Analytics Lake**
   - Locations: `derived/parquet/`
   - Policy: Deterministically rebuildable from Zone 1 evidence via `evallab.storage.parquet_compaction` and `evallab.evidence.facts`.
4. **Zone 4: Curated Marts & Evaluation Artifacts**
   - Locations: `derived/curated/`, `derived/comparisons/`, capability curves, eval cards
   - Policy: Structured domain views produced by `evallab.interpretation` and `evallab curve`.

---

## 4. Feature-Unblocked Status

Package 1 (Storage & Evidence Foundation) and Package 2 (Interpretation & Judgment Engine) are stabilized and locked. Infrastructure migration is complete. All current and future development is **FEATURE-UNBLOCKED** for active capability evaluations, difficulty screening, and automated feedback loops.
