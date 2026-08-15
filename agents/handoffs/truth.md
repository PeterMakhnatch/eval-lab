Status: building
Last: Implemented and focused-tested task bootstrap, power, family reports, and eval-card drafting.
Next: Run premerge/acceptance repeatedly, verify a fresh clone, then rebase and open the TRUTH PR.
Blockers: none

# TRUTH handoff

## Scope

- Honest task-level cohort comparisons and paired task inference.
- Power planning for detectable effects and n/k tradeoffs.
- Plain-language trajectory family reports from Parquet joined to canonical ATIF.
- Provenance-bearing eval-card templates and completed-spec drafts.

## Constraints observed

- Subscription credentials only. No API-key environment variables are introduced, read, or
  forwarded by this work.
- Raw Harbor jobs and registered task material remain read-only.
- Generated comparison/report artifacts stay rebuildable; durable eval-card drafts carry source
  digests and refuse overwrite.

## Evidence log

- `git fetch origin`: pass (2026-08-14).
- `uv sync`: pass with CPython 3.12.11 (sandboxed uv crashed in macOS system configuration;
  the same command succeeded outside that sandbox).
- Required repository guidance read before implementation.
- `uv run ruff check .`: pass.
- `uv run pytest`: pass, `90 passed in 7.03s`.
- `uv run pytest -q research/analysis/tests dashboard/tests`: pass after repairing stale
  post-REFRAME evidence paths, `33 passed`.
- `uvx ty@0.0.71 check src/ --output-format=concise`: expected nonzero under the ratchet,
  `28 diagnostics` (baseline/ceiling is 33; TRUTH adds zero and removes five local diagnostics).
- `uv run evallab compare research/analysis/control-oracle-vs-nop.json`: pass; the one-task
  control prints `not distinguishable / not comparable: only 1 paired task(s); at least 2 are
  required` instead of a ranking.
- Both `evallab power` modes render task-paired plans. Example fixed design: baseline 0.300,
  `n_tasks=100`, `k=3`, MDE `0.1428` per attempt (independent-attempt planning assumption is
  printed).
