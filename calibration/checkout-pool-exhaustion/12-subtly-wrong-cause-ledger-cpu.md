<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

ledger-db CPU climbed during the 14:00 hour and starved checkout of commit
capacity. checkout-api v4.19.0 had just raised workers, which increased the
query rate into an already hot database. We rolled back at 15:31 and CPU, then
errors, fell. Mitigated 15:38.

## Impact

94 minutes, ~7,000 failed checkouts, peak 38.1% 5xx.

## Timeline

- **14:02** — v4.19.0 workers 8→32, increasing ledger-db query concurrency.
- **14:04** — checkout 500s begin.
- **14:09** — page.
- **14:24** — `ledger-db` CPU warning at 72%.
- **15:31** — rollback reduces query concurrency.
- **15:38** — mitigated.

## Root Cause

ledger-db CPU saturation. The 14:24 warning shows CPU at 72% and still rising
through 14:45. With 32 workers, checkout issued more concurrent ledger writes
than the database could commit, so checkout requests waited and then timed
out. The `db.pool` warnings are a symptom of ledger-db not returning
connections promptly, not of an undersized application pool — 10 connections
had been enough for months.

The payments vendor is not involved (`vendor_payments_p99_ms` ~130ms).

## Contributing Factors

- No autoscale on ledger-db.
- Worker increase without a database capacity review.
- CPU page threshold at 90% is too high; 72% was already harmful.

## Corrective Actions

1. Scale ledger-db vertically and add a 60% CPU warning.
2. Keep checkout-api at 8 workers until ledger-db has headroom.
3. Add a DBA review gate on any worker-count change.
4. Lower the ledger-db page threshold from 90% to 70%.

## Open Questions

- What query was burning CPU? We do not have `pg_stat_statements` in the
  evidence pack.
- Should we shard ledger-db before the next peak?
