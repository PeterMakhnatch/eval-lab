# Publishing one finished job into the INDEX (HAR-237), 2026-10-30

**Verdict: publish is no longer a function of how many jobs the results home
holds.** Publishing one job into a mirror of the real home takes 22 ms at 1.4k
published jobs and 29 ms at 8.3k (best of 15); the same home costs 0.54 s and
3.4 s to rebuild. The splice the publish path uses writes the bytes a rebuild
writes: identical on every size measured here and on the real 8,287-job home.

[HAR-223](../process-job-timing-2026-10-29/README.md) measured
`results_home.write_index` at 98% of `evallab process-job` post-run wall time:
1.2-1.6 s warm at 8.2k published jobs, growing linearly, because every publish
re-read `provenance.json`, `config.json`, `lab-metadata.json` and
`processed/job.json` for every published job.

## What changed

`INDEX.md` and `INDEX-all.md` are now rendered from one model: a group per card
holding already-rendered row lines, newest row first.

- `write_index(home)` builds that model from every published job and rewrites
  both files. It stays as the repair path and for `backfill`, and its scan and
  write run under the home lock.
- `update_index(home, published)` — what `publish_job` calls — parses the
  published pair back into the model, splices the one job's rows (replacing its
  own previous rows, retiring the rows of a card rehome), and re-renders. It
  reads one job's records and rewrites the two files it must rewrite anyway.

The splice is only allowed to run on a file it can account for. The parser
accepts exactly what the renderers emit, and checks each card header against its
rows, the routine table against its counts, and the two files against each
other. Anything else — a missing or hand-edited INDEX, a count that disagrees
with its rows, a research doc that appeared since the last publish (which
changes the Research cell of every row of that card) — falls back to
`write_index`. So a splice can never publish a file a rebuild would not have
written; the fallback is also how a damaged INDEX repairs itself.

Ordering, locking and atomicity:

- Rows are keyed by `<day>-<job>` from the run link, not by absolute path, so a
  home reached through a symlink (`/tmp` and `/private/tmp`) splices the same
  way. Insertion is a binary search over rows that are already newest-first.
- The routine card table now lists cards in `INDEX-all.md` order (each card's
  newest entry) instead of by its newest *control* job. That order is readable
  back out of the published files, which is what lets one publish maintain the
  table without a sidecar. On today's home the two orders coincide: a rebuild
  of the real home reproduces both published files byte for byte.
- `INDEX.md` and `INDEX-all.md` are written through `.INDEX.md.tmp` +
  `os.replace`, and read-modify-write holds `flock` on `<home>/.index.lock`, so
  concurrent publishes serialize instead of interleaving. Two publishes that
  raced before could interleave two whole-file writes; now neither loses a row.

Nothing under `<home>/<day>/<job>/` is touched: published job bytes stay
immutable evidence, and the only new file in the home is the lock.

## Publish time against home size

`measure.py` mirrors the real home's INDEX inputs (the four files the INDEX
reads, one copy per published job) and measures `publish_job` of one small
staged job, and `write_index`, at four sizes, best of 7:

| Published jobs | publish (incremental) | INDEX rebuild | splice == rebuild |
|---|--:|--:|:--:|
| 1,373 | 19.4 ms | 403 ms | yes |
| 2,122 | 21.5 ms | 974 ms | yes |
| 4,213 | 44.3 ms | 2,061 ms | yes |
| 8,287 | 28.4 ms | 3,076 ms | yes |

The 4,213 row is a load spike, not a size effect: the publish number is not
monotonic, which is the point. Re-timed best-of-15 on the same mirrors after
that run, the two extremes were 22.2 ms (1.4k) and 29.1 ms (8.3k) against
542 ms and 3,442 ms for a rebuild. Publish grows by about 1 ms per 1,000
published jobs — the two markdown files are 0.55 kB per published job and both
have to be read and rewritten — where the rebuild reads 4 files per job and
grows by about 0.4 s per 1,000. Full numbers, including median and max:
[process-job-index-timing.csv](process-job-index-timing.csv).

Byte identity, three ways:

1. A rebuild of a mirror of the real home reproduces the results home's
   `INDEX.md` (816,621 bytes) and `INDEX-all.md` (3,712,162 bytes) exactly,
   once the mirror's resolved root replaces the real root in the run links.
   Those two files were written over months by the previous code path: the new
   renderer changed no cell, no row order and no count.
2. `splice == rebuild` at every size above, on mirrors built from real rows.
3. `tests/test_results_home.py` compares the two paths byte for byte after every
   publish in a small fixture: agent and control jobs in one card, a card
   published control-first (so the first `INDEX.md` has no agent runs), a job
   with a partial counts summary, a job with no processed report, republication,
   a card rehome, six concurrent publishes, a drifted count, a foreign
   `INDEX-all.md`, and a research doc published between two jobs.

## Reproduce

```bash
uv run python research/explorations/har237-incremental-index-2026-10-30/measure.py
```

The real home is only read (`~/Developer/eval-lab-results`, `--real-home` to
change it); the mirror lives under `/tmp/har237-measure` and one mirror is built
per run, with smaller homes made by parking whole day directories. Add
`--reuse` to re-measure against the mirror a previous run left behind.

## Limits

- Wall clock on a shared Mac under load 3.6-4.4 (other agents): best-of-N is
  reported with median and max, and the spread between sizes is run-to-run
  noise as often as it is size. The publish figure is a small job's publish
  (copy included); a job with a large trial tree adds its own copy time, which
  was never the INDEX.
- The guarantee is byte identity to a rebuild *for homes the publish path
  wrote*. A hand-edited row that keeps the counts plausible survives until the
  next rebuild; the header counts and every card are still checked, so the usual
  edit (an inserted, deleted or re-counted row) falls back instead.
- Only the row of the job being published is recomputed. A job whose published
  `processed/` report changed without republishing (the nightly refresh runs
  with `publish=False`) keeps its older row until it is republished or
  `backfill` rebuilds; before this change any publish would have refreshed it.
- The measurement's mirror holds the four INDEX inputs, not the full trial
  trees, so `publish_job`'s copy of the *staged* job is realistic but its
  neighbours are not: only the INDEX work is being priced.
