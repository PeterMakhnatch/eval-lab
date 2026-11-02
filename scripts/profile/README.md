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

## A breach is re-measured once

A median above `budget * (1 + tolerance_pct/100)` is not final. CI passes
`--reprofile` to `check_budgets.py`, which then re-profiles **only the breached
paths**, exactly once, with the same corpus pin, warmup and reps taken from the
report under test (`--paths` on the harness), and fails the run only when that
second median also exceeds the ceiling. A retry that clears prints a notice.
Both medians are written into `profile-report.json` under `remeasure`
(`first_median_ms`, `remeasure_median_ms`, `outcome`, `ceiling_ms`), so the
uploaded artifact shows what happened either way. There is no loop: one attempt
per breached path per run (`REMEASURE_PASSES`).

The policy exists because shared-runner spikes, not regressions, were failing
`main`: perf run 41147747627 measured `ingest` at 179 ms against a 172.5 ms
ceiling while the other 29 recent `main` runs measured 46-111 ms (median 71).
No budget value or tolerance changed; skip `--reprofile` to keep the strict
first-measurement behaviour.

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
