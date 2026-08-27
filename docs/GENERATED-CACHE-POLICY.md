---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Generated Products & Safe Cache Policy

This policy establishes the authoritative governance, regeneration commands, lifecycle classifications, and safe cleanup protocols for deterministic generated products, live operational runtime roots, immutable evidence stores, and cache artifacts across the `eval-lab` repository.

It anchors directly to the classifications, invariant rules, and cryptographic digests established in [docs/content-inventory.md](content-inventory.md) and [docs/git-estate-inventory.md](git-estate-inventory.md).

---

## 1. Module Location Stability & Invariant Guarantees

> **Authoritative Policy Statement**: Current module locations across `src/evallab/` and its subpackages (`evallab.storage`, `evallab.evidence`, `evallab.interpretation`, `evallab.schemas`, `evallab.recovery`) are **STABLE**. No file moves, renames, backwards-compatibility shims, or package directory reorganizations may be introduced. All new work, tools, and imports MUST use the authoritative current module paths directly.

- **No Code Splits / No Broad Restructuring**: No module splits, refactorings, or migrations are authorized within this policy.
- **AST Generator Scope Caveat**: `src/evallab/repomap.py` uses `src_dir.glob('*.py')` (shallow AST inspection) when compiling `docs/repo-map.md`. Subpackage modules residing under subdirectories (`src/evallab/storage/`, `src/evallab/evidence/`, `src/evallab/interpretation/`, `src/evallab/schemas/`, `src/evallab/recovery/`) are not displayed in `docs/repo-map.md`. This is a shallow generator trait, not an indicator of unused code. Every subpackage module has verified live consumers in `src/evallab/cli.py`, `dashboard/`, and the test suites.

---

## 2. Deterministic Generated Authorities & Regeneration Commands

The repository maintains several tracked and committed artifacts that are deterministic projections derived from underlying source code, test suites, database catalogs, or task registries. These files are managed by canonical Python generator entrypoints and custom Git merge drivers (`scripts/git-merge-regen.sh`).

| Generated Artifact Path | Canonical Generator Entrypoint | Consumer / Purpose | Verification & Check Command | Merge / Rebuild Driver |
|---|---|---|---|---|
| `docs/INDEX.md` | `python -m evallab.docindex generate` | Authoritative documentation index with frontmatter sha256 digests | `python -m evallab.docindex check` | Deterministic generator / `scripts/git-merge-regen.sh` |
| `docs/repo-map.md` | `python -m evallab.repomap generate` | Living repository symbol map and top-level module AST index | `python -m evallab.repomap check` | Deterministic generator / `scripts/git-merge-regen.sh` |
| `docs/STATUS.md` | `evallab status --write` | Daily research execution status and queue metrics snapshot | `evallab status` | Deterministic generator |
| `research/experiments/STATUS.md` | `evallab status --write` | Campaign-level status projection | `evallab status` | Deterministic generator |
| `research/lessons.md` | `python -m evallab.lessons` | Statistically gated empirical findings and Wilson 95% CI lessons | `python -m evallab.lessons` | Deterministic generator |
| `research/registration/inventory.json` | `evallab registry refresh` | Task registration inventory and certification ledger | `evallab registry audit --json` | Deterministic generator |
| `tests/fixtures/contracts/*.json` | Schema export via `evallab.schemas` | Golden JSON Schema contract fixtures for Pydantic models | `pytest tests/test_contracts.py` | Schema generator |
| `docs/diagrams/*.svg` | Mermaid CLI (`.mmd`) / Excalidraw Export | Visual architectural vector diagrams | Inspect vector rendering | Vector source render |

---

## 3. Projected Derived Lake & Analytical Subroots (`derived/`)

The `derived/` directory is a **mixed-authority** runtime estate consisting of immutable Content-Addressable Storage (CAS) payloads alongside rebuildable Parquet projections:

```text
derived/
├── evidence-cas/                  <-- [DURABLE RAW EVIDENCE] (Retention ∞; DO NOT DELETE)
│   └── 424 CAS payload blobs (sha256:0e55d5493205...)
├── parquet/                       <-- [REBUILDABLE PROJECTION] (7d raw -> compacted lake)
│   ├── z3_trajectories/
│   └── daily compact partitions
├── analyses/                      <-- [REBUILDABLE PROJECTION] (Trajectory markdown/json analyses)
├── interpretation/                <-- [REBUILDABLE PROJECTION] (Batch interpretation outputs)
├── interpretation_artifacts/      <-- [REBUILDABLE PROJECTION] (interpretation_artifacts.parquet)
├── machine_judgments/             <-- [REBUILDABLE PROJECTION] (machine_judgments.parquet)
├── acceptance_decisions/          <-- [REBUILDABLE PROJECTION] (acceptance_decisions.parquet)
└── reports/                       <-- [REBUILDABLE PROJECTION] (Markdown/JSON eval summaries)
```

### 3.1 Rebuildable Projection Subroots

| Subroot Path | Classification | File Count & Size (Snapshot) | Authority & Source | Rebuild Command | Retention Policy |
|---|---|---|---|---|---|
| `derived/parquet/` | `generated-rebuildable-projection` | 2,126 files (22.64 MB) | Raw trial logs and ATIF trajectories | `python -m evallab.storage.data_backfill --all` | 7-day granular retention $\rightarrow$ compacted daily tables via `evallab.storage.parquet_compaction` |
| `derived/analyses/` | `generated-rebuildable-projection` | 444 files (13.13 MB) | CAS evidence and evaluator models | `evallab analyze batch <inventory>` | Rebuildable projection; pruned alongside source trial GC |
| `derived/interpretation/` | `generated-rebuildable-projection` | 35 files (806 KB) | Trajectory IR and evaluator models | `evallab interpret batch` | Rebuildable projection; pruned alongside source trial GC |
| `derived/interpretation_artifacts/` | `generated-rebuildable-projection` | 1 file (45 KB) | Projected interpretation index | `evallab interpretation index rebuild` | Rebuildable projection cache |
| `derived/machine_judgments/` | `generated-rebuildable-projection` | 1 file (17 KB) | Machine judgment evaluation index | `evallab judgment index rebuild` | Rebuildable projection cache |
| `derived/acceptance_decisions/` | `generated-rebuildable-projection` | 1 file (21 KB) | Behavioral acceptance index | `evallab acceptance index rebuild` | Rebuildable projection cache |
| `derived/reports/` | `generated-rebuildable-projection` | 2 files (2 KB) | Markdown and JSON evaluation summaries | `evallab report generate` | Rebuildable report summaries |

---

## 4. Durable Raw Evidence & Zone 1 Retention Invariants (Retention $\infty$)

The repository enforces strict non-deletion invariants for ground-truth empirical assets. Automated janitorial tools, cleanup scripts, and maintenance tasks MUST NEVER delete or modify these roots.

### 4.1 Zone 1 Raw Evidence Stores
1. **`research/evidence/runs/*`**: Historical immutable execution traces and point-in-time benchmark records. Permanent retention ($\infty$).
2. **`library/benchmarks/_trajectories/*`**: Baseline trajectory goldens and task verification traces. Permanent retention ($\infty$).
3. **`derived/evidence-cas/` (`sha256:0e55d54932052223edde0b2613a6fee17fcfedd7ee378afa038237afb3e001ab`)**: Holds 424 immutable CAS payload blobs (3.99 MB) referenced by `TrajectoryIR`, `MachineJudgment`, and `AcceptanceDecision` pipelines. Deleting this root destroys unpromoted evaluation evidence.
4. **Untracked Unique Research Evidence**:
   - `research/experiments/manifests/cross-campaign-quality-summary.json` (`sha256:689922d6ef7ed69655858e4c8fa4d92c49303dc524f4f5d657486d6c6ed025c0`): Unique active research evidence. DO NOT DELETE.
   - `research/inbox/parked-glossary-evidence-2026-08-27.md` (`sha256:95f1cb05d036436360f0d5b9e672ecce1522066557f4a02cdb3b62b2bdce161f`): Unique historical resume evidence. DO NOT DELETE.

---

## 5. Operational State Roots & Retention Rules

Operational directories manage active runner execution, task scheduling, and local disaster recovery:

| Root Path | Classification | File Count & Size | Primary Lifecycle Role | Retention Policy | Prune / Deletion Mechanism |
|---|---|---|---|---|---|
| `runs/trial_jobs/` | `active-runtime` | 1,012 files (8.57 MB) | Zone 1 unpromoted execution traces across 47 active jobs | 14d uncompressed $\rightarrow$ 60d compressed $\rightarrow$ tombstone; permanent once promoted | **Sole Deleter**: `evallab gc --apply`. DO NOT delete wholesale. |
| `runs/.executor/` | `active-runtime` | 20 files (26 KB) | Harbor execution worker process logs | Ephemeral operational state | Trailing log rotation / `evallab gc` |
| `runs/scratch_and_tests/` | `active-runtime` | 257 files (392 KB) | Ephemeral test execution workspaces (`_smoke`, `_premerge`) | Ephemeral / purgeable after test runs | Safe to purge scratch workspaces; rebuildable via tests |
| `runs/specs/` | `active-runtime` | 3 files (1 KB) | Standalone root spec definitions (`mender-*`, `reframe-*`) | Active queue / runner specs | Version-controlled / re-creatable |
| `queue/` | `active-runtime` | 78 files (207 KB) | Active experiment task leases (`O_EXCL`) and append-only `events.jsonl` | Ephemeral task lifecycle state | Reset / re-enqueue specs. DO NOT delete while workers run. |
| `backups/postgres/` | `active-runtime` | 7 files (358 KB) | Nightly PostgreSQL database dumps | Trailing 14-day rolling window | Managed via nightly rotation (`evallab backups rotate`); restore via `pg_restore` |

---

## 6. Safe Cache Cleanup Rules & Ignore Protocols

### 6.1 Safe Ephemeral Cache Roots
The following artifacts and directories are safe to delete or clean at any time without data loss:
- `__pycache__/`, `*.pyc`, `*.pyo`: Python bytecode caches.
- `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`: Linter and test runner caches.
- `.coverage`, `htmlcov/`: Ephemeral test coverage reports.
- `runs/scratch_and_tests/`: Scratch directories created during local test runs.

### 6.2 Specific Root Ignores
- `/excalidraw.log`: Local untracked logging artifact generated by Excalidraw / Mermaid exports (`sha256:08ebb78843fcb2a4a8ec36ff066986a0f969d576a41f0341cacd52301e06aed0`). Ignored explicitly in `.gitignore` via `/excalidraw.log`.
- **Constraint**: Broad `*.log` ignore rules are **PROHIBITED** to prevent accidental masking of critical runner trial logs (`trial.log`) within experiment trees.

---

## 7. Agent Temporary Worktree Cleanup Gates

Eval Lab uses Git worktrees under `.worktrees/` for isolated subagent execution. Worktree lifecycle management is governed by the following cleanup gates:

1. **Active Worktree Protection**: Directories in `.worktrees/<name>` represent live concurrent agent work and MUST NEVER be deleted without explicit verification that the corresponding branch has been merged into `origin/main` or abandoned.
2. **Metadata Pruning Gate**: Stale administrative references in `.git/worktrees/` from previously removed checkouts may be cleaned safely at any time via:
   ```bash
   git worktree prune
   ```
3. **Worktree Directory Deletion Gate**:
   - Verify branch status: `git branch --merged origin/main`
   - Ensure working tree has no uncommitted changes or unique research evidence
   - Remove worktree explicitly:
     ```bash
     git worktree remove .worktrees/<worktree-name>
     git branch -d <branch-name>
     ```
