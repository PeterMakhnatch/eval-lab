# Experiment grid specifications

## Purpose
`grids/` contains declared experiment grid inputs for the LADDER exploration subsystem.
It defines parameter variation axes, constraint filters, and daily budget unit ceilings
for systematic agent and task evaluation.

## What lives here / entry points
- `grids/event-summary-elicitation.yaml`: Example elicitation experiment grid definition across task refs, agent controls, and preamble variations.
- Grid expansion command: `uv run evallab ladder generate grids/event-summary-elicitation.yaml`.

## Invariants or rules
- Declared experiment inputs: `grids/` holds declared experiment grid specs defining axes, constraints, and budget ceilings (cited in `agents/STRUCTURE.md` and `docs/ladder.md`).
- Controlled expansion: Grid expansion respects defined constraints and daily budget unit ceilings (cited in `docs/ladder.md`).
- One variable at a time: Grid specifications structure parameter variations systematically across tasks and models (cited in `AGENTS.md`).

## Tests or checks
- `uv run pytest tests/test_ladder.py`
- `uv run evallab ladder generate grids/event-summary-elicitation.yaml`

## What not to add here
- Do not store expanded run manifests or raw evaluation outputs here; use `runs/` or `research/evidence/`.
- Do not store execution outputs, trace caches, or unvetted data files here.
- Do not add unvalidated grid specifications lacking schema version or grid identifier.
