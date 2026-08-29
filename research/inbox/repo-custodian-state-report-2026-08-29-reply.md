---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-29
license_note: Private repository; internal repository-health analysis only.
status: distilled
feeds:
  - parked
---

# Repository Custodian State Report — Reply

**Date:** 2026-08-29
**Scope:** Static repository and GitHub-PR audit; no cloud, paid-model, Harbor, or full local test run.
**Evidence base:** `origin/main` at `3fc3c33f`; live checkout was `0e5b130c`, one local commit ahead and 31 commits behind `origin/main`; GitHub PR metadata queried 2026-08-29.

## Executive result

The repository has a sound evidence architecture and materially stronger quality
controls than its size suggests. The immediate quality risk is **control-plane
drift**, not raw-evidence accumulation or a need for broad source reorganization:
local `make check` omitted freshness checks already required by CI, and the
premerge type baseline documented on `main` still differed from CI while PR #260
remained open.

One safe cleanup was implemented: `make check` now runs the deterministic
`docindex` and `repomap` freshness checks before tests. It is isolated to
`Makefile`, has no benchmark/provider/evidence semantics, and does not overlap
the three open PRs (#260, #272, #230).

## 1. Structure, bloat, and evidence boundaries

### Measured estate

| Root | Files | Size | Classification / action |
|---|---:|---:|---|
| `derived/` | 3,034 | 38.79 MB | Mixed: preserve `derived/evidence-cas/` (424 immutable blobs; 3.99 MB); projections are rebuildable. |
| `library/` | 4,752 | 17.96 MB | Tracked immutable task supply; not bloat. |
| `tests/` | 464 | 9.99 MB | Executable contracts and golden fixtures; keep. |
| `src/` | 278 | 9.05 MB | Platform source; locations explicitly frozen. |
| `runs/` | 1,292 | 8.58 MB | Unpromoted runtime evidence; only `evallab gc --apply` manages trial data. |
| `research/` | 690 | 7.22 MB | Produced knowledge plus seven untracked inbox documents; needs ownership/promotion, not deletion. |
| `docs/` | 109 | 1.23 MB | Design and generated indexes. |
| `agents/` | 146 | 0.87 MB | Governance and historical handoffs. |

The audit counted **11,273 non-excluded files / 94.25 MB**, of which **6,380 are
tracked**. Size is not the urgent issue: immutable raw evidence is intentionally
retained, and runtime roots are ignored.

### Boundary findings

- `docs/GENERATED-CACHE-POLICY.md:31-40` correctly names deterministic generated
  authorities: `docs/INDEX.md`, `docs/repo-map.md`, two status projections,
  `research/lessons.md`, registration inventory, and schema fixtures.
- Zone 1 evidence must not be bulk-pruned: `research/evidence/runs/`,
  `library/benchmarks/_trajectories/`, and `derived/evidence-cas/`
  (`docs/GENERATED-CACHE-POLICY.md:77-87`).
- Rebuildable projections are `derived/parquet/` (2,126 files / 22.64 MB),
  `derived/analyses/` (444 / 13.13 MB), interpretation indexes, and reports
  (`docs/GENERATED-CACHE-POLICY.md:63-73`).
- The only safe runtime deletion surface is cache and scratch state such as
  `runs/scratch_and_tests/`, `.pytest_cache/`, and `.ruff_cache/`
  (`docs/GENERATED-CACHE-POLICY.md:106-117`). No cleanup was applied there
  because it is workstation state, not a reviewable repository change.
- An untracked `eval-lab-threat-model.md` (170 lines / 21.7 KB) violates the
  frozen root (`agents/STRUCTURE.md:3-8`) but is user-owned untracked work. It
  was deliberately not moved or committed.

### Giant files and duplicate/obsolete code

| Path | Lines | Disposition |
|---|---:|---|
| `src/evallab/task_workbench.py` | 4,575 | Defer: benchmark certification in active use. |
| `src/evallab/cli.py` | 4,176 | Defer: global command surface. |
| `src/evallab/authoring.py` | 3,620 | Defer: shared authoring qualification. |
| `src/evallab/interpretation/trajectory_runtime.py` | 2,486 | Defer: active data/interpretation substrate. |
| `src/evallab/schemas/__init__.py` | 2,167 | Defer: frozen contracts. |
| `src/evallab/queue.py` | 2,035 | Defer: execution state machine. |

No safe code deletion is supported. `agents/missions/ACTIVE.md:17-20` and
`docs/GENERATED-CACHE-POLICY.md:18-23` forbid physical package moves without new
Peter approval. The two TrajectoryIR modules remain a separately gated decision,
not a cleanup target.

## 2. Quality gates and CI

### Implemented controls

| Control | Evidence |
|---|---|
| Locked dependencies and Python matrix | `agents/CHECKS.md:9-17`; CI covers 3.12 and 3.14. |
| Lint / tests / types / governance | `agents/CHECKS.md:12-20`; `ci.yml`, `typecheck.yml`, `governance.py`. |
| Deterministic tests | Explicit host-state prohibition in `agents/CHECKS.md:28-34`. |
| Workbench and supply-chain controls | Dedicated offline oracle/nop/mutant workflows for FuncDAG, LOCA, agent-abstain, deep-planning, and Tau knowledge. |
| Generated-file integrity | `docindex check`, `repomap check`, custom merge driver, and post-merge/post-rewrite regeneration hooks. |
| Profile budget | `.github/workflows/perf.yml` uses isolated PostgreSQL service credentials. |
| Credential safety | `tau-knowledge.yml` uses unusable dummy OpenAI credentials; certification workflows use local oracle/nop controls. |

### Inconsistent or missing enforcement

1. **Type-gate drift.** CI uses `TY_BASELINE: 0` in
   `.github/workflows/typecheck.yml`; `scripts/premerge.sh` and
   `agents/CHECKS.md` on the assessed base still cite a 28-diagnostic ratchet.
   PR #260 corrects this but remains open. Until merged, local premerge can be
   green while CI is red.
2. **Local/CI asymmetry.** CI lint also runs docindex, registry audit, and
   lessons. Premerge does not run all three. `repomap` is only incidentally
   covered through tests, although generated-file hooks claim parity.
3. **Governance lane blind spot.** `governance.py` checks legacy live handoffs;
   ADR-031 tracks protected lanes in a free-form estate handoff. The mission
   board therefore has weaker structural verification than the rest of the
   governance system.
4. **No technical branch protection.** The private plan cannot enforce it;
   `gh pr checks <number>` on the exact head remains procedural
   (`agents/CHECKS.md:36-54`).

## 3. Stack discipline and recent PR sequence

### Instituted discipline

- One writer / isolated worktree is binding in `agents/WORKFLOW.md`; active
  protected lanes and package stability are stated in
  `agents/missions/ACTIVE.md:7-20`.
- Exact-head GitHub green—not local success, mergeability, or old CI—is the
  merge rule (`agents/CHECKS.md:36-54`).
- Generated indexes use `.gitattributes`, `scripts/git-merge-regen.sh`, and
  `.githooks/post-merge` / `post-rewrite` to regenerate rather than merge text.
- Current open work is narrow and non-overlapping: PR #260 (`scripts/premerge.sh`
  and OMP skills), #272 (agent orientation/context-pack docs), and #230
  (overnight ledger).

### Weak points exposed

- Hygiene changes lag rapid feature merges: PR #260 is the correct type-ratchet
  repair but remained unmerged after the CI contract already reached zero.
- PR #272 changes generated indexes while parallel work proceeds; generated files
  require fresh regeneration at the eventual merge head.
- Workflow setup boilerplate is duplicated across seven jobs. Pinned action SHAs
  are good; shared setup action/composite workflow should be a separate design
  change, not a cleanup drive-by.
- Generated benchmark verifier drift is mitigated by specialty workflows, but
  those workflows make broad matrix changes high blast radius. Preserve the
  oracle/nop/mutant and generated-artifact rejection controls.
- Evidence bloat is primarily intentional retention. Treating `derived/` as a
  disposable cache would destroy CAS evidence.

## 4. Prioritized cleanup list

| Priority | Item | Classification | Reason / dependency |
|---|---|---|---|
| P0 | Add doc-index and repo-map freshness checks to `make check` | **Safe now — implemented** | Deterministic, reversible, `Makefile` only. |
| P1 | Merge/review PR #260 then make premerge, checks documentation, and CI type baseline identical | Needs PR sequencing | Directly resolves CI/local type drift; do not overlap its open diff. |
| P2 | Add explicit governance validation for ADR-031 protected lane handoffs | Needs design + PR | Requires a stable machine-readable role-lane schema; do not parse free-form notes heuristically. |
| P3 | Add local equivalents for CI registry audit and lessons generation | Needs PR | Decide whether these belong in `make check`, `premerge`, or a named CI-parity target. |
| P4 | Deduplicate workflow setup boilerplate | Needs design PR | Preserve pinned SHAs and specialized credential/isolation lanes. |
| P5 | Promote or archive the seven untracked `research/inbox/` reports | Needs Research owner | They are useful context, not custodian-owned debris. |
| P6 | Decompose workbench/CLI/authoring monoliths | Defer | Explicit module-stability freeze and live benchmark coupling. |
| P7 | Consolidate `runs/`, `queue/`, `derived/`, `backups/` under another root | Defer | Deep external and runtime path contracts. |

## 5. Implemented safe cleanup

### Change

`Makefile` now makes `check` execute, in order:

```make
uv run ruff check .
uv run python -m evallab.docindex check
uv run python -m evallab.repomap check
uv run pytest
```

Help text now accurately says “lint, generated-index checks, and tests.”

### Evidence and scope

- Both new commands passed before the change on the assessed checkout.
- Neither `Makefile` nor its lines are touched by open PRs #260, #272, or #230.
- This does not change test selection, task/verifier semantics, provider
  credentials, cache retention, or generated artifacts.
- Full `make check` was intentionally not run: its existing final command runs
  the project-wide suite, prohibited for this local development loop.

### PR evidence

- Branch: `hygiene/make-check-indexes`
- Initial base: `origin/main` at `3fc3c33f`
- PR: [#290](https://github.com/PeterMakhnatch/eval-lab/pull/290)
- Initial cleanup head: `3a4dc82b`
- Focused validation passed: `docindex check`, `repomap check`, `make -n check`,
  `make help`, and `git diff --check`.
- The first exact-head CI run also exposed a missing inbox-frontmatter contract
  in this report; corrected on the next head. It additionally exposed an
  out-of-scope MCP base failure: trusted wheelhouse expects
  `joserfc-1.7.4` but resolves `1.7.5`, and recovery tests lack `cryptography`
  / `fastmcp`. These are not caused by `Makefile`.
