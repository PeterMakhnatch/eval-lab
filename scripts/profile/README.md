# SPEED profiling harness

One command, six paths, FORGE measurement rules (`docs/engineering.md` §4).

```bash
uv run python scripts/profile/harness.py
```

Writes a markdown report to stdout and `runs/_speed/profile-report.{md,json}`
(gitignored `runs/`). Does not start Harbor. Does not write the shared
`evallab` catalog. Ingest uses a scratch database named `evallab_speed_prof`
(or `$EVAL_LAB_PROFILE_DATABASE_URL` if set).

```bash
uv run python scripts/profile/check_budgets.py runs/_speed/profile-report.json
```

`--cpu-only` skips the scratch-Postgres connect and still drives
`database.ingest_job` against a recording connection (pytest / no-daemon).
`--inject-ms PATH=N` adds N milliseconds to one path (ratchet proof).
