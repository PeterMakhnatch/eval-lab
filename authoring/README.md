# Task authoring templates

## Purpose
`authoring/` contains versioned authoring templates and seed specifications for task
synthesis. It defines the discrete variation axes (category, scenario, difficulty)
used by the synthesis pipeline to generate evaluable tasks.

## What lives here / entry points
- `authoring/templates/category.yaml`: Domain category taxonomy, exemplar references, and topics.
- `authoring/templates/scenario.yaml`: Instruction style archetypes, registers, and prompt constraints.
- `authoring/templates/difficulty.yaml`: Complexity tier definitions, operational bounds, and anti-patterns.

## Invariants or rules
- Decoupled axes: Category, scenario, and difficulty files remain orthogonal data definitions (cited in `docs/authoring.md`).
- Version-controlled templates: Authoring specifications are committed, reviewable seed material (cited in `agents/STRUCTURE.md`).
- Anti-pattern enforcement: Tasks avoid artificial obscurity, unseeded flakiness, and oracle leakage (cited in `docs/authoring.md`).

## Tests or checks
- `uv run pytest tests/test_authoring.py`
- `uv run pytest tests/test_authoring_properties.py`

## What not to add here
- Do not store generated tasks or proposals here; use `library/tasks/` or `library/synthetic/`.
- Do not place ephemeral run artifacts, caches, or unvetted seed data here.
- Do not add language runtimes or non-YAML configuration files.
