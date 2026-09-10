---
title: "Harbor Native Track: Task Pack Loader Decision & Policy Recommendations"
date: "2026-09-06"
author: "BuilderWave0 (eval-lab harbor-native buildout)"
scope: "TerminalWorld (TW) memory conflict & FACET task naming under Harbor 0.21.0"
governance: "derived/harbor-packs/MANIFEST.md & contamination-claim-boundaries.md"
---

# Harbor Task Pack Loader Decision & Recommendations

## 1. Executive Summary

Both **TerminalWorld** (`EuniAI/TerminalWorld`) and **FACET-Terminal** (`StoKou/FACET-Terminal`) exhibit data incompatibilities when loaded by Harbor 0.21.0. Under our binding governance contract:
- **Pack bytes are IMMUTABLE**: We NEVER rewrite downloaded files in `derived/harbor-packs/*` or `library/external/*`. Doing so would invalidate cryptographic digest pins (`7355ea41…`), break upstream repeatability, and fork downstream trials.
- **Decision Pattern**: We recommend a two-tier resolution:
  1. File precise **Upstream Issues** against both repositories to fix root-cause release pipelines.
  2. Implement **Our-Boundary Loader Shims** (<30 lines each) inside `src/evallab/loader_shims.py` with explicit value-choice policies, applied strictly at load/staging time (shadow directory mirroring).

---

## 2. Pack 1: TerminalWorld (TW) Resource Allocation Conflict

### The Defect
12 of 20 sampled tasks declare conflicting memory values:
```toml
[environment]
memory = "2G"      # Deprecated legacy string -> parses as 2048 MB
memory_mb = 4096   # Modern integer field -> 4096 MB
```
Harbor 0.21.0's `EnvironmentConfig` validates that if both fields are present, their converted values must be equal. Because `2048 != 4096`, Harbor raises `ValueError` and refuses to load the task.

### Evaluation: Upstream Issue vs. Our-Boundary Shim
- **Upstream Issue**: Filed as artifact `UPSTREAM-ISSUE-tw-memory-conflict.md`. Recommended for upstream maintenance, but external response times are indefinite.
- **Our-Boundary Shim**: Recommended for lab execution. The conflict is a clean 1-line schema deprecation artifact.
- **Value-Choice Policy**:
  - **Policy**: `modern_memory_mb_precedence`.
  - **Rationale**: `memory_mb` represents the author's modern, calibrated allocation (4096 MB). The legacy `memory = "2G"` was a carryover default. Dropping the legacy string satisfies Harbor 0.21.0's schema migration cleanly without down-sizing container memory.
  - **Implementation**: Implemented in `src/evallab/loader_shims.py::resolve_tw_memory_conflict` (14 lines), tested in `tests/test_loader_shims.py`.

---

## 3. Pack 2: FACET-Terminal Task Naming Collision

### The Defect
Every `task.toml` across all 6,020 tasks defines `name = "FACET-Terminal"`. Harbor 0.21.0 requires names to match `^[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+$` (`org/name`). Single-component names cause `TaskModel.is_valid_dir` to return `False`, dropping the entire pack.

### Evaluation: Upstream Issue vs. Our-Boundary Shim
- **Upstream Issue**: Filed as artifact `UPSTREAM-ISSUE-facet-task-name.md`. Crucial because the upstream generator `harbor-template/task.toml` has a static string instead of templated `{{ task_id }}`.
- **Our-Boundary Shim**: Recommended for lab execution when FACET lanes run.
- **Value-Choice Policy**:
  - **Policy**: `namespaced_slug_per_task`.
  - **Rationale**: Rewriting `name = "facet/<task_id>"` (where `task_id` is the stable archive directory name, e.g., `task_000001`) satisfies Harbor's `org/name` requirement while ensuring unique trial directory identities, avoiding collision in traces and results.
  - **Implementation**: Implemented in `src/evallab/loader_shims.py::resolve_facet_task_name` (14 lines), tested in `tests/test_loader_shims.py`.

---

## 4. Verification and Compliance Matrix

| Pack | Defect | Upstream Status | Shim Status | Line Count | Choice Policy |
|---|---|---|---|---|---|
| TerminalWorld | `memory='2G'` vs `memory_mb=4096` | Documented in `UPSTREAM-ISSUE-tw-memory-conflict.md` | Implemented in `loader_shims.py` | 14 lines (<30) | `memory_mb` precedence (4096 MB wins) |
| FACET-Terminal | `name='FACET-Terminal'` no org prefix | Documented in `UPSTREAM-ISSUE-facet-task-name.md` | Implemented in `loader_shims.py` | 14 lines (<30) | Namespaced as `facet/<task_id>` |
