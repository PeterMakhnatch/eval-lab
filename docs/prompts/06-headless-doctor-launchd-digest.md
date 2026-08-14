# 06 — Headless doctor, launchd, digest

`doctor --headless` (Keychain item readable, `~/.codex/auth.json` present,
Docker reachable, Postgres up, disk headroom — booleans only, never values;
reuse the migrated `with-claude-auth` sourcing pattern). `evallab schedule
install` writes two LaunchAgent plists (`…tick` every 30 min, `…nightly` at
02:30) running `zsh -lc 'cd <repo> && uv run evallab …'` in the user
session. `evallab digest` renders yesterday from the catalog + events into
`digests/`.

Acceptance: with launchd loaded and no human present, a queued oracle control
runs, ingests, and appears in the next morning's committed digest; with the
Keychain locked, the digest reports quarantine and zero dispatch.

## Repository-wide constraints

- Preserve immutable `runs/` and rebuildable PostgreSQL.
- Keep deterministic extraction before model analysis.
- Put every new JSON contract in `src/evallab/schemas.py` as a Pydantic
  model.
- Add dependencies only with `uv add`; `uv.lock` is authoritative.
- The executor is the only application code path that may invoke Harbor or
  Docker.
- Never print, log, persist, or inspect credential values; health surfaces
  contain booleans only.
- No billable run in tests; stub the runner. Live checks use only Oracle/no-op.
