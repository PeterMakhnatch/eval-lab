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

`--scratch-dir <path>` moves the files the `projection`, `facts`, `digest` and
`queue-tick-100` paths write (parquet exports, digest and queue roots) away from
`<output-dir>/scratch`; `ingest` still goes through the Postgres service's disk. CI passes
`/dev/shm/evallab-speed`: `projection` and `facts` publish each table with a durable
replace (16 and 24 fsyncs per rep), so on a shared runner their medians tracked the
disk's latency, not the code (18 runs of unchanged code: `projection` 4.9-225.5 ms,
`facts` 11.6-104.5 ms; 42 runs on tmpfs: 0.9-3.2 and 4.0-9.6 ms). The committed budgets
assume that: on Linux pass `--scratch-dir /dev/shm/<dir>` before `check_budgets.py`
(a macOS laptop is fine without it). The report stays in `--output-dir`. The sync-write
cost the gate no longer times is pinned by `tests/test_profile_harness.py`. Evidence:
`budgets.json` notes.

`--cpu-only` skips the scratch-Postgres connect and still drives
`database.ingest_job` against a recording connection (pytest / no-daemon).
`--inject-ms PATH=N` adds N milliseconds to one path (ratchet proof).
