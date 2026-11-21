# Experiment grid specifications

## Purpose
`grids/` contains declared experiment grid inputs for the LADDER exploration subsystem.
It defines parameter variation axes, constraint filters, and daily budget unit ceilings
for systematic agent and task evaluation.

## What lives here / entry points
- `grids/event-summary-elicitation.yaml`: Example elicitation experiment grid definition across task refs, agent controls, and preamble variations.

## Invariants or rules
- Controlled expansion: Grid expansion respects defined constraints and daily budget unit ceilings.
- One variable at a time: Grid specifications structure parameter variations systematically across tasks and models (cited in `AGENTS.md`).

## What not to add here
- Do not store expanded run manifests or raw evaluation outputs here; use `runs/` or `research/evidence/`.
- Do not store execution outputs, trace caches, or unvetted data files here.
- Do not add unvalidated grid specifications lacking schema version or grid identifier.
