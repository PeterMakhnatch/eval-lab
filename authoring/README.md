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
- Decoupled axes: Category, scenario, and difficulty files remain orthogonal data definitions.
- Authoring specifications are versioned seed material.
- Anti-pattern enforcement: Tasks avoid artificial obscurity, unseeded flakiness, and oracle leakage.

## What not to add here
- Do not store generated tasks or proposals here; use `library/tasks/`.
- Do not place ephemeral run artifacts, caches, or unvetted seed data here.
- Do not add language runtimes or non-YAML configuration files.
