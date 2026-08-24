Status: done
Last: merged as PR #89 (`d988935`)
Next: none
Blockers: none

# Navigation Handoff (WS-F)

Generated, fail-closed map of `src/evallab/` plus the three operator skills
from `docs/build-plan.md` line 158.

## Leased Files

- `src/evallab/repomap.py`: `python -m evallab.repomap generate|check`.
  AST-derived modules, CLI attribution, queue / Parquet / DuckDB / Postgres
  stores. Marker `<!-- generated-by: repomap v1 -->`. No timestamp.
- `tests/test_repomap.py`: determinism, every module listed, CLI attribution,
  stale-map and missing-docstring check failures, check pass on the real
  tree, skill front-matter.
- `docs/repo-map.md`: generated artifact, written by the generator.
- `docs/INDEX.md`: regenerated so `docindex check` stays green.
- `.claude/skills/lab-status/SKILL.md`
- `.claude/skills/mission-launch/SKILL.md`
- `.claude/skills/review/SKILL.md`
- `agents/handoffs/navigation.md`: this handoff.

## Behaviour

- `generate [-o docs/repo-map.md]`: deterministic map. Purpose comes from
  the module docstring's first sentence, else argparse description, else a
  public definition docstring. `check` fails when a module has nothing to
  describe or the committed map is stale.
- Skills cite only commands that exist: `evallab status`, `evallab preflight`,
  `evallab digest`, `python -m evallab.contextpack build`, `gh pr checks`.
  There is no `evallab context` command.

## Verification

- `uv run pytest tests/test_repomap.py` (8 passed)
- `uv run pytest` (full suite green, 1 xfailed — pre-existing)
- `uv run ruff check .` (clean)
- `uv run python -m evallab.docindex generate` (wrote `docs/INDEX.md`)
- `uv run python -m evallab.docindex check` (pass)
- `uv run python -m evallab.repomap generate` (wrote `docs/repo-map.md`)
- `uv run python -m evallab.repomap check` (pass)
