# Board Notes (Audits)

- 2026-08-18 [M015-audit]: `agents/handoffs/preflight.md` claimed documentation in `docs/operations.md`, but `docs/operations.md` contains no section or mention for `evallab preflight`. Recommend Platform lane / LOOP-SURFACE add preflight CLI instructions to `docs/operations.md`.
- 2026-08-18 [M015-audit]: `src/evallab/status_generator.py` targets `research/experiments/STATUS.md` by default, whereas night loop expectations target `docs/STATUS.md`. Also, no CLI entrypoint exposes `status_generator` directly. Recommend LOOP-SURFACE wire CLI and align target path.
