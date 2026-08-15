Status: building
Last: P3 + P4 implemented and locally validated (4-zone provenance; 10 DuckDB intelligence queries)
Next: P5 synthetic-task design, then public ATIF fetch prototype + live sample verification
Blockers: `uv sync --locked` panics in macOS SystemConfiguration; existing locked .venv imports all required dependencies

# DATA-STRATEGY handoff

Mission: trajectory intelligence, literature mapping, curation architecture
(P1–P5). Standing orders: subscriptions only, no API-key env vars; premerge +
fully green gh checks before merge; never touch registered/* or loosen policy/.

## Ownership notes (declared up front)

- `docs/research/` (new), `docs/data-architecture.md` (new): nominally
  BUILDER-owned bucket; created here on Peter's explicit mission order.
- `src/evallab/schemas.py`: BUILDER-owned shared file; mission explicitly
  orders ProvenanceMetadata here. Diff will be additive-only (one new model +
  exports), premerge-validated.
- `research/analysis/queries.sql`: ANALYST/TRUTH path; mission orders 8+ new
  queries. I append a clearly delimited section; existing queries untouched.
- New role row added to `agents/ROLES.md` in the PR (per "new role = new row").

## Session log — 2026-08-15

- 7b333b6 checked out into .worktrees/data-strategy, branch role/data-strategy;
  `uv sync` clean.
- agents/CHECKS.md, scripts/premerge.sh, docs/architecture.md all exist and were
  read; merge gate is satisfiable (ty ratchet 33, premerge = local CI parity).
- Confirmed targets: docs/research/ does not exist yet; research/analysis/
  queries.sql exists (will append); src/evallab/schemas.py exists with
  ContractModel convention (pydantic, frozen contracts).

## P1 evidence (2026-08-15)

- Mission citation arXiv:2502.12151 verified WRONG (resolves to "VoLUT",
  volumetric video streaming). Real literature surveyed instead: 2602.07150
  (variance: sigma>1.5pp at temp 0, 9 runs to detect 2pp), 2603.05344 (harness
  variable catalog), 2607.05775 (six failure clusters), 2604.17596 (Terminal
  Wrench). TB 4.0 confirmed nonexistent; lineage is TB2 -> TB2.1 -> TB3 ==
  Frontier-Bench v0.1 (74 tasks).
- Deliverable: docs/research/literature-survey.md, commit 62a5ff2. Nine
  takeaways T1-T9 mapped to lab components (cohort sigma reporting, Parquet
  fields, synthetic-task gate floor).

## P2 evidence (2026-08-15)

- harborframework/terminal-bench-2.0 fetched README [verified]: read-only TASK
  mirror (Apache-2.0, LFS), not trajectories — catalog corrected accordingly.
- Cataloged: Harbor-Index 1,476 trials (Hub), obaydata/mcp-agent-trajectory-
  benchmark (49 traj, ATIF v1.2), yoonholee/terminalbench-trajectories;
  Tier B OpenHands corpora at scale (nvidia SWE-Zero 318k, Open-SWE-Traces
  200k+, nebius SWE-agent 80,036, Nemotron-SWE 59k, SWE-Hero 34k, SWE-rebench).
- Deliverable: docs/research/external-datasets.md with TrajectorySourceBackend
  seam spec: commit-SHA pins only, anonymous-only (no HF_TOKEN — subscriptions
  rule), material_digest + license + audit reuse, external Parquet isolated
  under derived/parquet/external/.

## P3/P4 evidence (2026-08-15)

- `ProvenanceMetadata` and nine deterministic contract tests cover all four
  zones, digest validation, transform versioning, lineage, and extra-field
  refusal; `pytest -q tests/test_provenance.py` → `9 passed`.
- `docs/data-architecture.md` defines storage boundaries, admission gates,
  allowed transitions, query/publication rules, and rebuild invariants.
- `docs/research/trajectory-intelligence.md` defines Loop Index, Tool
  Efficiency Ratio, Context Bloat Velocity, and four explicitly provisional
  failure buckets.
- Ten named DuckDB statements appended to `research/analysis/queries.sql`;
  `pytest -q tests/test_trajectory_queries.py tests/test_provenance.py` →
  `10 passed`; Ruff and `git diff --check` pass.

## P3 evidence (2026-08-15)

- ProvenanceMetadata appended to src/evallab/schemas.py (additive; BUILDER
  file touched on explicit mission order). tests/test_provenance.py:

```
$ uv run pytest tests/test_provenance.py -q
.........                                                                [100%]
9 passed
```

- Full suite after change: 101 passed (72%+28% progress bars, 0 failures).
- docs/data-architecture.md revised to normative form (zones, admission
  gates, allowed transitions, rebuild invariants) during review; committed as
  P3.1.

## P4 evidence (2026-08-15)

- Live-store validation (DuckDB over derived/parquet in the main checkout,
  read-only), correct statement splitter:

```
DS-1: OK rows=5   DS-2: OK rows=0   DS-3: OK rows=0   DS-4: OK rows=0
DS-5: OK rows=0   DS-6: OK rows=46  DS-7: OK rows=2   DS-8: OK rows=0
DS-9: OK rows=0   DS-10: OK rows=1  DS-11: OK rows=1  DS-12: OK rows=0
12/12 DS queries validate against the live Zone 02 store
```

- Zero-row results are expected: the local corpus is dominated by oracle/nop
  controls whose trajectories carry no tool calls. DS-6 classifies all 46
  trials; DS-1 sample:

```
local-lab/event-summary            oracle  adhoc          28  1.0  0.0
local-lab/event-summary            nop     adhoc           2  0.0  0.0
petermakhnatch/transaction-recon.  oracle  adhoc           1  1.0  0.0
petermakhnatch/transaction-recon.  codex   gpt-5.6-terra   3  0.0  0.0
terminal-bench/html-js-filter      codex   gpt-5.6-terra   3  0.0  0.0
```

- DS-7 flagged event-summary digests where rewards disagree (0.0 vs 1.0):
  inspection shows oracle-vs-nop splits — a true candidate correctly resolved
  by the documented interpretation boundary (agent difference, not verifier
  flakiness). The candidate/adjudication split works as designed.
