Status: done
Last: merged as PR #78 (`53ad483`)
Next: none
Blockers: none

# Context Pack Compiler Handoff (WS-B)

Implements `docs/build-plan.md` WS-B ("CONTEXT: pack compiler") providing deterministic, audience-filtered context bundles for eval-lab missions.

## Leased Files
- `src/evallab/contextpack.py`: Context pack compiler engine, doc discovery, front-matter parser, CRAFT task facet integration, mission brief templates, SHA-256 content hashing, and CLI (`build` and `list-docs`).
- `tests/test_contextpack.py`: 31 tests covering determinism, audience/status filtering, front-matter variants, CRAFT facets/patterns, mission briefs, CLI, and repo doc front-matter integrity.
- `docs/context-packs.md`: Living specification for context pack compiler, doc front-matter standard, and CLI usage.
- `docs/*.md` & `docs/research/*.md`: Front-matter metadata additions (`status: living|historical`, `audience: [builder|analyst|runner|operator]`) across all repository documentation without altering prose.
- `agents/handoffs/context-pack.md`: This handoff record.

## Verification
- `uv run pytest tests/test_contextpack.py` (31 passed in 0.15s)
- `uv run pytest` (909 passed, 1 xfailed in 22.7s)
- `uv run ruff check .` (clean, 0 diagnostics)
- `uv run python -m evallab.contextpack list-docs` (lists all docs with status/audience)
- `uv run python -m evallab.contextpack build builder` (deterministic compilation, hash `sha256:fa7b8e97a749ef62f25fcf25ddab418fb75f0e586235d0635fc210b866d30868`)
- `uv run python -m evallab.contextpack build builder --task terminal-bench/atrx-vep-crispr` (resolves hybrid verifier, pytest/golden signals, clean-room anti-cheat patterns from `craft.parquet`)
