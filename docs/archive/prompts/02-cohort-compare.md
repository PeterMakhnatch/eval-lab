---
status: historical
audience:
  - builder
  - analyst
---

> **Archived work order**: Completed historical brief. Living contract: docs/analysis-loop.md. Board: agents/missions/ACTIVE.md.

# Build deterministic cohort comparison

## Mission

Add a deterministic comparison command that turns a declared pair or set of
Harbor trial cohorts into machine-readable facts and a concise Markdown report.
This component must work without an LLM and must make accidental
apples-to-oranges comparisons visible.

Work on a named branch/worktree after the ATIF indexing component is complete.
Read `AGENTS.md`, `docs/architecture.md`, `docs/analysis-loop.md`, and the current
experiment matrix and database schema first.

## Required behavior

1. Define and validate a versioned cohort-comparison spec. It must identify:
   - input job/trial selectors;
   - task and verifier identity constraints;
   - fixed agent/model/environment conditions;
   - the one intended differing variable;
   - reward dimensions and deterministic metrics to summarize;
   - optional pairing key and selection rules.
2. Refuse a causal-style report when task digest, verifier digest, or more than
   one declared consequential variable differs. Offer an explicit `exploratory`
   mode that labels those limitations in both JSON and Markdown.
3. Produce JSON containing cohort membership, exclusions with reasons,
   denominators, pass counts/rates, reward summaries, exception counts,
   duration/token/cost summaries, and available trajectory/tool-use summaries.
4. Produce Markdown from the JSON; do not calculate results independently in
   two code paths.
5. Link each aggregate to the source trials and list representative successes,
   failures, and infrastructure errors. Use deterministic selection rules.
6. Show raw counts for small samples. Do not claim statistical significance or
   broad model capability merely from a pass-rate difference.
7. Support reading directly from raw/evidence directories. PostgreSQL may
   accelerate selection but cannot be required for correctness.
8. Write generated reports under ignored `derived/comparisons/`; promotion to a
   tracked report remains a separate reviewed action.

## Tests

Use fixtures for:

- a valid single-variable comparison;
- mismatched task digest;
- mismatched verifier/environment identity;
- infrastructure failures excluded from capability denominator but reported;
- paired and unpaired trials;
- missing reward dimensions;
- deterministic output ordering;
- identical JSON on repeated runs over identical inputs;
- exploratory mode carrying explicit validity warnings.

## Interface

A good interface is:

```bash
uv run evallab compare experiments/comparisons/example.json
```

The exact syntax may change if current CLI conventions require it, but the input
spec and generated JSON/Markdown paths must be explicit.

## Acceptance commands

```bash
uv run pytest
uv run ruff check .
uv run evallab compare <fixture-or-example-spec>
```

## Handoff

Document the comparison schema, denominator rules, exploratory warnings, and
one example report. State exactly what the report establishes and what it does
not. Do not invoke a model during implementation or validation.

## Implemented contract (ANALYST, 2026-08-14)

- `evallab compare <spec>` reads raw jobs, writes deterministic JSON and a
  Markdown rendering under `derived/comparisons/`, and needs no database or
  model. `research/analysis/control-oracle-vs-nop.json` is the tracked example.
- Causal mode fixes task and verifier identity and refuses any consequential
  difference beyond `declared_variable`; exploratory mode preserves every
  mismatch as a validity warning in both outputs.
- Exceptions stay beside `n_total` and are excluded from the capability
  denominator; missing rewards are reported separately. Pass@1 uses every
  eligible trial; task-level outcomes for k > 1 use stable trial-UUID selection.
  Both carry Wilson 95% intervals. Groups short of k are explicit exclusions.
  Paired reward deltas use the declared task key and do not claim significance.
- The tracked n=1 controls establish only that Oracle scored 1.0 and no-op 0.0
  under the same task/verifier/environment. Their Wilson intervals are shown to
  make the small sample unmistakable; this is not a model-capability estimate.
