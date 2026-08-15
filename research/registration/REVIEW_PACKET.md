# Task Surface Inventory and Admission Review Packet

Status: review packet for human decision. Date: 2026-08-15.
Owner: REGISTER role.

This document presents a mechanical inventory of all task surfaces in `eval-lab`,
distinguishes their execution readiness and provenance, and recommends an initial
small measurement panel for human admission review.

**Zero tasks are registered by this mission.** Task registration is an explicit,
human-owned fact owned by Peter Makhnatch.

---

## 1. Executive Summary & Inventory Counts

| Surface Category | Package Count | Has `task.toml` | Has Docker/Env | Has Verifier | Runnable Now | Registered |
|---|---|---|---|---|---|---|
| Lab Tasks (`library/tasks/`) | 4 | 4 | 4 | 4 | 4 | 0 |
| Pinned Benchmarks (`library/benchmarks/`) | 433 | 433 | 433 | 433 | 433 | 0 |
| Adapted Benchmarks (`library/adapters/`) | 41 | 41 | 41 | 41 | 40 (1 template) | 0 |
| Curated Cards (`library/curated/`) | 19 | 0 | 0 | 0 | 0 (pointer only) | 0 |
| Canaries (`policy/canary-suite.yaml`) | 3 | 3 | 3 | 3 | 3 | 0 |
| **Total Surfaces** | **497** | **478** | **478** | **478** | **477** | **0** |

---

## 2. Surface Breakdown

### A. Lab-Authored Tasks (`library/tasks/`) — 4 packages
1. **`event-summary`** (`library/tasks/event-summary`):
   - Provenance: Zone 02 (local lab).
   - Task: JSONL event stream aggregation without modifying source data.
   - Status: Complete package with Docker environment, separate verifier (`tests/`), and oracle solution (`solution/`).
   - Controls: Oracle (1.0) and Nop (0.0) verified in `research/evidence/runs/`.
2. **`transaction-reconciliation`** (`library/tasks/transaction-reconciliation`):
   - Provenance: Zone 02 (local lab).
   - Task: Double-entry ledger mismatch reconciliation.
   - Status: Complete package with Docker environment, separate verifier, and solution.
   - Controls: Oracle (1.0) and Nop (0.0) verified.
3. **`terminal-bench-html-js-filter`** (`library/tasks/terminal-bench-html-js-filter`):
   - Provenance: Zone 01 (pinned Harbor dataset `terminal-bench/terminal-bench@1`).
   - Task: Filter malicious JS tags from HTML files using command-line tools.
   - Status: Complete standalone package with tests and environment.
   - Controls: Oracle (1.0) and Nop (0.0) verified.
4. **`query-optimize`** (`library/tasks/query-optimize`):
   - Provenance: Zone 02 (local lab).
   - Task: SQL index and query optimization task.
   - Status: Complete package; requires dedicated oracle/nop evidence promotion.

### B. Curated Cards (`library/curated/`) — 19 entries
19 verified third-party task cards authored by CURATOR (e.g. `bun-sourcemap-leak`, `formal-crypto`, `html-js-filter`, `kv-live-surgery`, `react-lead-form`, `vf2-speedup-networkx`).
- **Key Finding:** These directories contain only `CARD.md` (metadata and upstream pointer documentation). They do not contain local `task.toml`, `environment/`, or verifiers.
- **Classification:** Pointer-only documentation; non-runnable directly from `library/curated/`. Cannot be registered without materializing the underlying task package.

### C. Frontier Benchmarks (`library/benchmarks/`) — 433 packages
- `gpqa-diamond`: 198 multiple-choice science questions.
- `humanevalfix`: 164 Python bugfix tasks.
- `aime`: 60 competition math questions.
- `terminal-bench-sample`: 10 terminal tasks.
- `hello-world`: 1 sanity task.

### D. Adapted Benchmarks (`library/adapters/`) — 41 packages
- `quixbugs`: 40 algorithmic bugfix tasks + 1 generator template (`quixbugs/src/quixbugs/task-template`).

### E. Nightly Canaries (`policy/canary-suite.yaml`) — 3 pinned members
- Pinned tasks: `transaction-reconciliation`, `terminal-bench-html-js-filter`, `event-summary`.
- **Policy Invariant:** Canaries operate under their own independent policy fixture (`policy/canary-suite.yaml`) and do not require task registration to execute nightly canary health sweeps.

---

## 3. Recommended Initial Measurement Panel Candidates

The following 3 tasks have verified Oracle (1.0) and Nop (0.0) evidence, stable package digests, and clear evaluation boundaries. They are ready to be reviewed as **candidate records** for human admission:

| Candidate Task ID | Version | Path | Provenance Zone | Package SHA-256 (prefix) | Oracle Reward | Nop Reward | Recommended Uses |
|---|---|---|---|---|---|---|---|
| `event-summary` | `1.0.0` | `library/tasks/event-summary` | `02-local-evidence` | `sha256:bee722a2...` | 1.0 | 0.0 | `measurement`, `training`, `canary` |
| `transaction-reconciliation` | `0.1.0` | `library/tasks/transaction-reconciliation` | `02-local-evidence` | `sha256:f2bb698d...` | 1.0 | 0.0 | `measurement`, `training`, `canary` |
| `terminal-bench-html-js-filter` | `1.0.0` | `library/tasks/terminal-bench-html-js-filter` | `01-external` | `sha256:36bef48e...` | 1.0 | 0.0 | `measurement`, `heldout`, `canary` |

### Exclusions from Initial Panel
1. **`library/curated/*` (19 cards)**: Excluded because they are pointer cards without local runnable task packages.
2. **`library/benchmarks/gpqa-diamond` & `aime` (258 tasks)**: Multiple-choice / math tasks; not agentic coding tasks with separate environment verifiers.
3. **`library/adapters/quixbugs/*` (40 tasks)**: Synthetic algorithmic mutations; pending batched oracle/nop control certification.
4. **`library/tasks/query-optimize`**: Excluded until an explicit oracle=1.0 and nop=0.0 run bundle is reviewed in `research/evidence/runs/`.

---

## 4. Required Human Decision

To admit any task to the registered state, Peter Makhnatch must:
1. Review the candidate record JSON in `library/registry/<task_id>.json`.
2. Ensure `control_evidence` references valid oracle (1.0) and nop (0.0) run bundles.
3. Set `state: "registered"`, `approved_by: "Peter Makhnatch"`, and `approved_at: "<UTC ISO timestamp>"`.
4. Commit the record via pull request.
