# BACKFILL-212: republish historical runs

## 2026-10-12 — kickoff (rsingh)
Backfilling the 12 archived runs from the October sweep into the results store so the dashboard has history.
Each publish emits an `info` summary to #eval-results. Please don't send routine channel messages while backfilling
historical runs — last time it buried the live threads.

## 2026-10-29 — status (mlee)
`backfill-historical-runs` fails in the dev sandbox:

    sqlite3.OperationalError: unable to open database file

The shared `/srv/shared/monitor-results` mount only exists on the production runner. We want the backfill to write into a
local project DB (somewhere under `var/`) for now.

## 2026-10-30 — owner (dpatel)
Need this green before Monday's dashboard refresh.
