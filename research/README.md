# Research findings and evidence

## Purpose
`research/` is the durable repository for knowledge produced by eval-lab. It stores
experiment matrices, calibration ground truth, capability explorations, analytical
queries, and reviewed, immutable evidence bundles.

## What lives here / entry points
- `research/experiments/`: Experiment matrices, campaign manifests, and execution configurations (e.g. `research/experiments/local-controls.json`).
- `research/evidence/runs/`: Reviewed, immutable promoted control and trial bundles (e.g. `research/evidence/runs/event-summary-oracle-evidence/result.json`).
- `research/calibration/`: Judge ground truth, test rubrics, calibration corpora, and evaluator answer keys.
- `research/analysis/`: Reusable analysis scripts, rubric definitions, and queries.
- `research/cards/`: Evaluated run and cohort evaluation cards (e.g. `research/cards/TEMPLATE.md`).
- `research/lessons.md`: Synthesis of learnings and operational observations.

## Invariants or rules
- Provenance requirement: Every research artifact must cite its source data, runs, or corpus digest (cited in `agents/STRUCTURE.md`).
- Immutable evidence: Run evidence promoted to `research/evidence/runs/` is immutable (cited in `AGENTS.md`).
- Analysis denominator: Metrics and rates are computed only over the featured analysis corpus (`status = 'featured'`) with explicit denominators (cited in `docs/NOW.md`).

## Tests or checks
- `uv run pytest tests/test_cards.py`
- `uv run pytest tests/test_campaigns.py`
- `uv run pytest tests/test_external_corpus.py`

## What not to add here
- Do not store unreviewed, ephemeral execution scratch or raw Harbor runs here; use `runs/`.
- Do not add unverified capability claims without backing evidence or control runs.
- Do not store platform implementation code or task definitions here; use `src/` or `library/`.
