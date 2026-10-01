---
name: lab-status
description: >
  Summarize the current state of the eval lab from the three operator
  surfaces. Use when Peter asks what is happening, whether it is safe to
  run, what the digest or STATUS.md says, or for the morning routine.
---

# Lab status

Read these three surfaces. Do not start Harbor, paid models, or a tick.

## 1. Live snapshot — `uv run evallab status`

Read-only. Recent jobs, queue Now/Next, tasks, store health, saved analysis.

```bash
uv run evallab status
uv run evallab status --json
uv run evallab status --update   # writes <primary>-state/reports/STATUS.md (external); add --output docs/STATUS.md to promote the snapshot
```

How to read: `observed` is on disk; `unavailable` is a missing store, not
zero; `draft` analysis is unreviewed; `review-needed` is malformed or
invalid. Health names `postgres`, `phoenix`, `queue`, `parquet`.

Stale: a snapshot from another checkout, `--from` pointing at smoke
scratch, or Health `unavailable` while you treat the numbers as current.

## 2. Can we run? — `uv run evallab preflight`

Read-only. Quota per paid provider, unfinished queue by purpose, power
warnings. Exit `1` when a provider reading refuses billable work.

```bash
uv run evallab preflight
uv run evallab preflight --useful-effect 0.15
```

How to read: `[unavailable]` / `UNKNOWN` is not headroom. Quota is
account-wide. Finished queue states are omitted. No comparison queued
means no power warning, not a clean bill of health.

Stale: a reading whose age is hours old (printed on the block); treating
UNKNOWN as permission; pooling every queued comparison as one cohort.

## 3. Digest and STATUS.md

```bash
uv run evallab digest --help
```

- Daily digest: live copy at `<primary>-state/reports/<YYYY-MM-DD>.md`, written by
  `uv run evallab digest` (and nightly); `digests/<YYYY-MM-DD>.md` holds only
  snapshots promoted with an explicit `--output`. Rendering never commits.
  What ran, spend, preflight copy, what waits on Peter.
- Discoveries: `digests/DISCOVERIES.md` is a curated ledger, not a live
  snapshot. Do not regenerate or commit it from this skill.
- Program status: live copy at `<primary>-state/reports/STATUS.md`, written by
  `uv run evallab status --update` (implemented in `status_generator.py`);
  `docs/STATUS.md` is the promoted snapshot (`--output docs/STATUS.md`).
  Note: `research/experiments/STATUS.md` is a historical August 2026 snapshot.

Stale: yesterday's digest read as today; STATUS.md whose "today" is not
the calendar day you think; a digest that still embeds a preflight from
an older queue; a live external report mistaken for the promoted snapshot.

Report the three surfaces, then say which readings are stale or
unavailable. Do not invent counts the commands did not print.
