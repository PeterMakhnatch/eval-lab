# Daily operator digests

## Purpose
`digests/` stores committed daily summary reports curated for the operator. Each
digest captures fleet state, trial completions, discovered anomalies, and scheduled
garbage collection plans.

## What lives here / entry points
- `digests/2026-09-05.md`: Example daily digest report for operator review.
- `digests/DISCOVERIES.md`: Curated registry of verified research findings and evidence links.
- Render command: `uv run evallab digest` (renders today's digest).

## Invariants or rules
- Committed one-pager: Daily digests are committed derived reports curated for human review (cited in `agents/STRUCTURE.md` and `AGENTS.md`).
- Truthful fleet state: Digest generation names only files that exist, and never reports finished missions as active fleet state (cited in `tests/test_unattended.py`).
- Garbage collection integration: Daily digests append the nightly garbage collection plan (cited in `src/evallab/cli.py` and `tests/test_gc.py`).

## Tests or checks
- `uv run evallab digest`
- `uv run pytest tests/test_digest.py`
- `uv run pytest tests/test_unattended.py`

## What not to add here
- Do not store raw run outputs, execution logs, or unparsed JSON traces here; use `runs/` or `research/evidence/`.
- Do not commit speculative, unverified claims without evidence links.
- Do not store automated scraper dumps or unreviewed intermediate tables.
