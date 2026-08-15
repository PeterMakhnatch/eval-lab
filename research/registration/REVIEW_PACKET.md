# Task Surface Inventory and Admission Review Packet

Status: review packet for human decision. Date: 2026-08-15.
Owner: REGISTER role.

This document presents a mechanical inventory of all task surfaces in `eval-lab`,
distinguishes their execution readiness and provenance, and recommends an initial
small measurement panel for human admission review based strictly on durable evidence.

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

## 2. Evidence Contract & Verification Bar

Per the canonical task registration contract (`docs/task-registry.md`), any task admitted to the `registered` state must cite existing, durable promoted control evidence with:
1. Exact repository-relative path to evidence JSON (`evidence_path`).
2. Cryptographic SHA-256 digest of the evidence file (`evidence_digest`).
3. UTC timestamp when the trial was observed (`observed_at`).
4. Parsed verification proving the expected agent (`oracle` / `nop`), exact reward (`oracle == 1.0`, `nop == 0.0`), and task identity matching.

Missing evidence, unpromoted runs, tampered digests, or non-matching rewards fail closed.

---

## 3. Surface Breakdown & Evidence Status

### A. Lab-Authored Tasks (`library/tasks/`) — 4 packages
1. **`event-summary`** (`library/tasks/event-summary`):
   - Provenance: Zone 02 (local lab).
   - Package: Complete package with Docker environment, separate verifier (`tests/`), and oracle solution (`solution/`).
   - Durable Control Evidence (Promoted in `research/evidence/runs/`):
     - **Oracle**: `research/evidence/runs/event-summary-oracle-evidence/result.json` (SHA-256 `sha256:94008ac5b3559dbade582a0ad3373a5f56957438f5621ce72fe77e94ec28229e`, observed 2026-08-13T20:33:44.112624Z, reward = 1.0).
     - **Nop**: `research/evidence/runs/event-summary-nop-evidence/result.json` (SHA-256 `sha256:bf7787daa7360fed39fd975f2adb03025a6d157d8fd41fb222e1d55f34dfb1a8`, observed 2026-08-13T20:33:54.832213Z, reward = 0.0).
   - Admission Status: **Ready for candidate review as initial measurement task**.

2. **`transaction-reconciliation`** (`library/tasks/transaction-reconciliation`):
   - Provenance: Zone 02 (local lab).
   - Status: Complete package with Docker environment, separate verifier, and solution.
   - Evidence Status: **Downgraded from initial panel**. Historical exploratory runs exist, but no promoted run bundle currently resides in `research/evidence/runs/`. Must execute a promoted oracle (1.0) and nop (0.0) control run bundle before registration review.

3. **`terminal-bench-html-js-filter`** (`library/tasks/terminal-bench-html-js-filter`):
   - Provenance: Zone 01 (pinned Harbor dataset `terminal-bench/terminal-bench@1`).
   - Status: Complete standalone package with tests and environment.
   - Evidence Status: **Downgraded from initial panel**. Pinned canary verification exists, but no standalone promoted control bundle is filed in `research/evidence/runs/`. Must be promoted with verified oracle/nop evidence before registration review.

4. **`query-optimize`** (`library/tasks/query-optimize`):
   - Provenance: Zone 02 (local lab).
   - Evidence Status: Excluded pending oracle/nop control run promotion.

### B. Curated Cards (`library/curated/`) — 19 entries
19 verified third-party task cards authored by CURATOR.
- Contain only `CARD.md` (metadata/upstream documentation).
- Non-runnable pointer cards without local `task.toml` or verifiers. Cannot be registered without materializing the full task package.

### C. Frontier Benchmarks (`library/benchmarks/`) — 433 packages
- Pinned datasets (`gpqa-diamond`, `humanevalfix`, `aime`, `terminal-bench-sample`, `hello-world`).
- Non-admitted for registration pending task-by-task control verification.

### D. Adapted Benchmarks (`library/adapters/`) — 41 packages
- `quixbugs`: 40 bugfix tasks + 1 generator template.
- Non-admitted pending batched oracle/nop control certification.

### E. Nightly Canaries (`policy/canary-suite.yaml`) — 3 pinned members
- Pinned tasks: `transaction-reconciliation`, `terminal-bench-html-js-filter`, `event-summary`.
- **Policy Invariant:** Canaries operate under independent policy fixture (`policy/canary-suite.yaml`) and do not require task registration to execute nightly canary health sweeps.

---

## 4. Recommended Initial Measurement Panel Candidate

Based strictly on promoted, durable evidence on disk:

| Candidate Task ID | Version | Path | Provenance Zone | Package SHA-256 (prefix) | Oracle Evidence (Promoted) | Nop Evidence (Promoted) | Allowed Uses |
|---|---|---|---|---|---|---|---|
| `event-summary` | `1.0.0` | `library/tasks/event-summary` | `02-local-evidence` | `sha256:bee722a2...` | `research/evidence/runs/event-summary-oracle-evidence/result.json` (`1.0`) | `research/evidence/runs/event-summary-nop-evidence/result.json` (`0.0`) | `measurement`, `training`, `canary` |

---

## 5. Required Human Decision

To admit `event-summary` (or any future task) to the registered state, Peter Makhnatch must:
1. Review the candidate record JSON in `library/registry/<task_id>.json`.
2. Verify that `control_evidence` references valid promoted oracle (1.0) and nop (0.0) run bundles with matching sha256 digests.
3. Set `state: "registered"`, `approved_by: "Peter Makhnatch"`, and `approved_at: "<UTC ISO timestamp>"`.
4. Commit the record via pull request.
