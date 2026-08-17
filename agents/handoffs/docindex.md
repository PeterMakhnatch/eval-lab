Status: review-wanted
Last: implement docs/INDEX.md generator and archive sweep (WS-E item 7)
Next: Integrator review and merge into main
Blockers: none

# Docindex Handoff (WS-E Item 7)

Implements `docs/build-plan.md` line 146: `docs/INDEX.md` generator + archive
sweep, front-matter driven.

## Leased Files

- `src/evallab/docindex.py`: `python -m evallab.docindex generate|check`.
  Reuses `contextpack.parse_doc` / `DocMetadata` / `parse_front_matter`; does
  not reimplement front-matter parsing.
- `tests/test_docindex.py`: determinism, audience/status grouping, check
  failures (missing front-matter, invalid status, stale index), check pass on
  the real `docs/` tree.
- `docs/INDEX.md`: generated artifact, written by the generator, not by hand.
  Front-matter `status: living` with all four audiences. Marker
  `<!-- generated-by: docindex v1 -->`. No timestamp.
- `agents/handoffs/docindex.md`: this handoff.

## Behaviour

- `generate [-o docs/INDEX.md]`: deterministic index of `docs/*.md` plus
  `docs/research/*.md` (same corpus as contextpack). Grouped by audience, then
  status. Historical docs also appear in an Archive section.
- `check`: fail-closed. Nonzero exit when a discovered doc is missing
  front-matter, has a status outside (`living`, `historical`), has an audience
  outside (`builder`, `analyst`, `runner`, `operator`), or when the committed
  index is stale versus a fresh generation.

## Verification

- `uv run pytest tests/test_docindex.py` (7 passed)
- `uv run pytest` (979 passed, 1 xfailed)
- `uv run ruff check .` (clean)
- `uv run python -m evallab.docindex generate` (wrote `docs/INDEX.md`)
- `uv run python -m evallab.docindex check` (pass on the real tree)
