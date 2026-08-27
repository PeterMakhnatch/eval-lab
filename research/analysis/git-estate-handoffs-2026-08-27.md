---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Git Estate Handoffs & Quarantined Assets Ledger — 2026-08-27

Repository current main 5c707900; primary reconciliation is complete. Current module locations are stable for now; new work uses the current authoritative paths.

This document records all unmerged, dirty, untracked, unpushed, and unknown Git assets across the `eval-lab` estate that require active handoff, operator review, or permanent preservation.

---

## 1. Protected Active Lanes & Worktrees (DO NOT TOUCH)

| Lane / Path | Head SHA | Status / Purpose |
|---|---|---|
| `/Users/petermakhnatch/Developer/eval-lab` | `5c707900` | Primary working tree on `main` |
| `.worktrees/lane-architect` | `c2269f1` | Active architect lane; gates PR #230 (lift ADR-030 Ops hard stop) |
| `.worktrees/lane-execution` | `933b8a3` | Active execution lane with watchdog / execution drivers |
| `.worktrees/lane-storage` | `68278fc` | Active storage lane for CAS and database management |
| `.worktrees/hygiene-git-estate-inventory` | `5c707900` | Active hygiene estate inventory worktree |

---

## 2. Dirty & In-Progress Worktrees (12 Entries — Handoff Required)

The following worktrees contain uncommitted modifications, staged items, or untracked files and MUST NOT be deleted:

| Worktree Path | Head SHA | Dirty State Evidence |
|---|---|---|
| `/private/tmp/eval-lab-factor-provenance` | `0ad6446` | 10 modified tracked files, 1 staged change (`sql/*`, `src/evallab/*`) |
| `/private/tmp/eval-lab-portable-registry-71993` | `2178311` | 10 modified tracked files, 1 staged change (`.github/workflows/ci.yml`, `src/evallab/registry.py`) |
| `/private/tmp/eval-lab-lessons-boundary-20260823` | `2178311` | 2 modified tracked files, 1 staged change (`sql/lessons.sql`, `src/evallab/lessons.py`) |
| `/private/tmp/eval-lab-hardening` | `9a5cdf7` | 1 untracked file (`excalidraw.log`) |
| `/private/tmp/eval-lab-run-synthetic-wave` | `98c21b5` | 1 untracked directory (`specs/`) |
| `/private/tmp/eval-lab-wave-shared` | `bb73dc7` | 1 modified tracked file, 1 staged change (`src/evallab/semantic_facts.py`) |
| `/private/tmp/eval-lab-wave-tau` | `27c7ca5` | 1 untracked directory (`trials/`) |
| `.worktrees/canary-runs-ir-v1` | `8c996cb` | 1 modified tracked file, 1 staged change, 2 untracked files |
| `.worktrees/data-backfill-command` | `ecdceff` | 1 staged change, 3 untracked files (`src/evallab/data_backfill.py`, `excalidraw.log`) |
| `.worktrees/tbench3-screen` | `79dd74a` | 4 untracked spec files in `research/experiments/specs/terminal-bench-v3-screen/` |
| `.worktrees/trajectory-interpretation-v1` | `ec77a96` | 2 modified files, 1 staged file, 7 untracked fixtures/tests (`src/evallab/acceptance.py`) |
| `.worktrees/trajectory-platform-interpretation-v1` | `3315005` | 1 untracked file (`excalidraw.log`) |

---

## 3. Unmerged-Useful Assets & Retained Snapshots

### Pull Request #217 (`fix/a2-completion-claim-classifier`)
- **Status**: Closed draft on HOLD per architecture directive.
- **Head Ref**: `origin/fix/a2-completion-claim-classifier` (commit `0c2552c`).
- **Preservation Reason**: Contains unique 27-case adversarial regression test suite and 6-round review brief for A2 completion claim classifier redesign. Not superseded by any merged implementation.

### Preserved Primary Evidence & Context
- [`research/analysis/preserved-primary-evidence-AGENTS-2026-08-27.md`](preserved-primary-evidence-AGENTS-2026-08-27.md): Verbatim copy of divergent primary evidence file (SHA-256: `f9a82e52cbebb23c9b815caf70f37e395d0e10f1d1ad75b817a76333459607b5`). Reconciles the untracked evidence note from the primary working tree.

### Preserve Baseline Snapshots
- `preserve/main-local-2026-08-16` / `origin/preserve/main-local-2026-08-16`: 2026-08-15 lab digest snapshot.
- `preserve/main-local-digests` / `origin/preserve/main-local-digests`: 2026-08-16 lab digest snapshot.
- `preserve/pre-sync-2026-08-16` / `origin/preserve/pre-sync-2026-08-16`: Pre-sync baseline snapshot.
- `feature/recovery-bench-salvage` / `origin/feature/recovery-bench-salvage`: Recovery bench salvage artifacts.

### Untracked Research Baseline Manifests
- `research/experiments/manifests/cross-campaign-quality-summary.json`: 158 KB untracked CAS quality summary.
- `research/inbox/parked-glossary-evidence-2026-08-27.md`: Terminology audit evidence cited in overnight ledger.

---

## 4. Quarantined Unknown Historical Branches

Historical mission branches without automated commit ancestry in `origin/main` or replacement PR markers (across local and remote references). These are quarantined and retained pending domain review.

---

## 5. Machine-Readable Cross-Reference

Every entity cataloged in this handoff document corresponds to an exact row in [`research/analysis/git-estate-inventory-2026-08-27.json`](git-estate-inventory-2026-08-27.json).
