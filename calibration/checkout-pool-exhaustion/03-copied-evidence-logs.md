<!-- calibration-variant: copied-evidence -->
# Postmortem: INC-2291

## Summary

INC-2291 affected checkout-api on 2026-07-14. Here is what the logs show.


2026-07-14T13:47:02Z INFO  startup: checkout-api v4.18.3 workers=8 db_pool_max=10 acquire_timeout_ms=5000
2026-07-14T13:52:41Z INFO  vendor.payments: POST /charge 200 in 126ms
2026-07-14T13:58:19Z INFO  vendor.payments: POST /charge 200 in 133ms
2026-07-14T14:02:11Z INFO  startup: checkout-api v4.19.0 workers=32 db_pool_max=10 acquire_timeout_ms=5000
2026-07-14T14:02:44Z INFO  vendor.payments: POST /charge 200 in 129ms
2026-07-14T14:03:58Z WARN  db.pool: acquire slow wait_ms=812 active=10 max=10 waiters=6
2026-07-14T14:04:07Z WARN  db.pool: acquire slow wait_ms=1904 active=10 max=10 waiters=23
2026-07-14T14:04:12Z ERROR http: POST /v1/checkout 500 TimeoutError: connection pool exhausted (max=10, waiters=41) after 5000ms

## Impact

The metrics for the incident window are below.

bucket_end_utc,checkout_requests,checkout_5xx,http_5xx_rate_pct,http_p99_ms,db_pool_wait_p99_ms,db_pool_active,db_pool_max,vendor_payments_p99_ms,ledger_db_cpu_pct
2026-07-14T13:45:00Z,1180,2,0.2,238,3,4,10,131,41
2026-07-14T13:50:00Z,1204,3,0.2,241,3,4,10,129,42
2026-07-14T13:55:00Z,1191,2,0.2,236,4,5,10,133,41
2026-07-14T14:00:00Z,1213,3,0.2,244,4,5,10,130,43
2026-07-14T14:05:00Z,1248,76,6.1,3812,1450,10,10,129,63
2026-07-14T14:10:00Z,1236,227,18.4,5001,2104,10,10,133,68
2026-07-14T14:15:00Z,1229,331,26.9,5002,2388,10,10,132,69
2026-07-14T14:20:00Z,1241,410,33.0,5003,2596,10,10,128,70
2026-07-14T14:25:00Z,1218,429,35.2,5001,2733,10,10,131,70
2026-07-14T14:30:00Z,1225,441,36.0,5002,3050,10,10,134,71
2026-07-14T14:35:00Z,1232,451,36.6,5004,2988,10,10,130,71

## Timeline


14:12 <r.okafor> paged on checkout 5xx, anyone else seeing it
14:13 <s.lindqvist> yep, dashboard is red
14:15 <s.lindqvist> roughly 18% of POST /v1/checkout failing, all with 500s
14:18 <r.okafor> customers are complaining in #support too
14:20 <r.okafor> payments vendor status page just posted "elevated latency" — probably them
14:21 <r.okafor> opening a ticket with the vendor
14:23 <s.lindqvist> hmm, our vendor call p99 is 130ms and flat all afternoon, doesn't look like them
14:26 <r.okafor> keeping the vendor theory for now, it's the only thing that changed externally
14:29 <r.okafor> anything deployed today?
14:31 <s.lindqvist> checkout-api v4.19.0 went out at 14:02. search-api also shipped at 14:05
14:33 <r.okafor> search-api isn't in the checkout path, ignore that one
14:36 <s.lindqvist> the 500s are all TimeoutError, not vendor errors
14:41 <r.okafor> vendor advisory cleared and we're still erroring, so it isn't them
14:44 <s.lindqvist> ledger-db cpu warning fired too, but it's at 70 and pages at 90

## Root Cause

The root cause is visible in the logs and the deploy history:

2026-07-10T11:20:00Z,checkout-api,v4.18.1,add structured request logging,ci-bot
2026-07-13T09:41:00Z,checkout-api,v4.18.3,dependency bumps only; no configuration change,ci-bot
2026-07-14T14:02:00Z,checkout-api,v4.19.0,raise HTTP server worker count from 8 to 32 for throughput,ci-bot
2026-07-14T14:05:00Z,search-api,v2.2.9,refresh bundled TLS certificate authorities,ci-bot
2026-07-14T15:31:00Z,checkout-api,v4.18.3,rollback of v4.19.0,r.okafor


2026-07-14T13:47:02Z INFO  startup: checkout-api v4.18.3 workers=8 db_pool_max=10 acquire_timeout_ms=5000
2026-07-14T13:52:41Z INFO  vendor.payments: POST /charge 200 in 126ms
2026-07-14T13:58:19Z INFO  vendor.payments: POST /charge 200 in 133ms
2026-07-14T14:02:11Z INFO  startup: checkout-api v4.19.0 workers=32 db_pool_max=10 acquire_timeout_ms=5000
2026-07-14T14:02:44Z INFO  vendor.payments: POST /charge 200 in 129ms

## Contributing Factors

14:52 <r.okafor> trying a restart of two instances, no change
15:12 <s.lindqvist> restart didn't help, errors identical after
15:26 <r.okafor> going to roll back v4.19.0, we're out of other ideas
15:31 <r.okafor> rollback started
15:38 <r.okafor> errors are gone, calling it mitigated
15:40 <s.lindqvist> we should figure out what in v4.19.0 did this, the diff is tiny

## Corrective Actions

The following configuration is currently in effect and should be reviewed:

release: v4.18.3

server:
  # v4.19.0 changed this value to 32. The rollback restored it to 8.
  workers: 8
  request_timeout_ms: 8000

database:
  host: ledger-db.internal
  pool:
    # Per-instance connection pool. Last changed 2025-11-04, when the service
    # ran 8 workers. Not touched by v4.19.0.
    max_connections: 10
    acquire_timeout_ms: 5000
    idle_timeout_ms: 30000

vendor:
  payments:
    base_url: https://api.payments-vendor.example
    timeout_ms: 3000
    retries: 2

## Open Questions


2026-07-14T09:02:11Z INFO  backup-lag: nightly ledger-db backup finished 11m late
2026-07-14T14:07:30Z INFO  search-api-cert-expiry: TLS certificate for search-api expires in 21 days
2026-07-14T14:09:10Z PAGE  checkout-api-5xx-rate: 5xx rate 18.4% > 5% for 5m
2026-07-14T14:24:00Z WARN  ledger-db-cpu: ledger-db CPU 70% > 70% for 10m (page threshold is 90%)
2026-07-14T15:39:40Z INFO  checkout-api-5xx-rate: resolved
