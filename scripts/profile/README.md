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

## The profiled corpus is pinned

`harness.DEFAULT_CORPUS` names two Harbor job directories
(`event-summary-nop-evidence`, `event-summary-oracle-evidence`), **not** the
directory `research/evidence/runs`. Three of the six paths — `ingest`,
`projection`, `facts` — take the loaded job list, so profiling a directory
measured however much evidence happened to be committed and every promotion
moved the gate. `scripts/profile/budgets.json` declares that corpus shape under
`corpus`, and `check_budgets.py` refuses any report measured against a
different one.

Use `--corpus <repo-relative-path>` (repeatable) for ad-hoc profiling against
anything else. Such a report will fail `check_budgets.py` on purpose: the
committed budgets only describe the pinned corpus, and a run on another corpus
is not a valid re-baseline sample either.

`--cpu-only` skips the scratch-Postgres connect and still drives
`database.ingest_job` against a recording connection (pytest / no-daemon).
`--inject-ms PATH=N` adds N milliseconds to one path (ratchet proof).
