# ruff: noqa: E501
"""Additional labeled postmortems beyond the harbor-practice seeds."""

from __future__ import annotations

CHECKOUT: dict[str, tuple[str, str]] = {}
RETRY: dict[str, tuple[str, str]] = {}


def _add(store: dict[str, tuple[str, str]], doc_id: str, variant: str, body: str) -> None:
    store[doc_id] = (variant, body.strip() + "\n")


# ---------------------------------------------------------------------------
# checkout-pool-exhaustion
# ---------------------------------------------------------------------------

_add(
    CHECKOUT,
    "07-correct-metrics-led",
    "correct",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

INC-2291 is a configuration mismatch made visible by `checkout-api v4.19.0`.
The 14:02 deploy raised HTTP workers from 8 to 32. The per-instance pool stayed
at `max_connections: 10`. Metrics then show `db_pool_active` pinned at 10 and
`db_pool_wait_p99_ms` climbing from 4 to 3104 until rollback to `v4.18.3` at
15:31. Mitigation was declared at 15:38.

## Impact

- Window 14:04–15:38 (94 minutes).
- About 7,021 failed `POST /v1/checkout` requests (`metrics.csv` `checkout_5xx`
  sum over the degraded buckets).
- Peak 5xx rate 38.1% in the 14:45 bucket; window failure rate about 29.5%.
- `http_p99_ms` sat on the 5000ms acquire timeout for the degraded period.

## Timeline

- **14:02** — v4.19.0 deploy; startup line `workers=32 db_pool_max=10`.
- **14:04** — first `TimeoutError: connection pool exhausted (max=10, waiters=41)`.
- **14:05** — 5xx 6.1%, `db_pool_wait_p99_ms` 1450, `db_pool_active` 10.
- **14:09** — `checkout-api-5xx-rate` pages at 18.4%.
- **14:20** — vendor advisory adopted as a hypothesis.
- **14:41** — advisory clears; our 5xx curve does not.
- **14:45** — peak: 38.1% 5xx, wait p99 3104.
- **15:31** — rollback to v4.18.3 (`workers=8`).
- **15:38** — incident mitigated.

## Root Cause

Workers=32 against a pool of 10 is the mechanism. `service-config.yaml` still
has `database.pool.max_connections: 10` and `acquire_timeout_ms: 5000` from
2025-11-04, when the service ran 8 workers. The v4.19.0 startup line and every
`db.pool` warning (`active=10 max=10`) plus the `TimeoutError` text establish
that requests queued for a connection and died at 5000ms. Rollback restoring
8 workers cleared the waiters without touching the database.

The payments-vendor advisory is not the cause: `vendor_payments_p99_ms` stayed
127–134ms, vendor calls returned 200, errors started at 14:04 (before 14:20)
and continued after 14:41. search-api TLS at 14:05 is a different service.
ledger-db CPU at 72% is below the 90% page and rises after the errors.

## Contributing Factors

- No alert on pool saturation or acquire wait (`alerts.log`).
- Pool size is not derived from worker count, so a one-line worker change
  passed review.
- `deployment.canary: false` rolled the change to all six instances.
- The vendor hypothesis was held from 14:20 to 14:41 despite flat vendor
  latency noted at 14:23.

## Corrective Actions

1. Couple `database.pool.max_connections` to `server.workers`, or fail startup
   when `workers > max_connections`.
2. Re-size the pool for 32 workers and re-release v4.19.0 under load.
3. Page on `db_pool_active >= db_pool_max` for 5 minutes and warn on
   `db_pool_wait_p99_ms` above 250ms.
4. Set `deployment.canary: true` for checkout-api.

## Open Questions

- Were any customers charged without an order confirmation? Reconciliation is
  not in the supplied evidence.
- Why was the worker count raised to 32? No ticket is supplied.
- Do other services share the fixed pool-sizing assumption?
""",
)

_add(
    CHECKOUT,
    "08-correct-config-led",
    "correct",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

`service-config.yaml` and the v4.19.0 startup line disagree about capacity:
32 HTTP workers, 10 database connections, 5000ms acquire timeout. That
configuration, shipped at 14:02, is why `POST /v1/checkout` returned 500 until
the 15:31 rollback to v4.18.3. INC-2291 was mitigated at 15:38.

## Impact

Checkout was degraded for 94 minutes. Roughly 7,000 requests failed (7,021
`checkout_5xx` in `metrics.csv`). Peak 5xx was 38.1% at 14:45. Successful
requests were still slow because `http_p99_ms` sat at ~5000.

## Timeline

All times UTC, 2026-07-14.

- **14:02** — `deploys.csv`: raise workers 8 → 32. Log: `workers=32 db_pool_max=10`.
- **14:03–14:04** — `db.pool` `active=10 max=10`; first exhausted timeout at 14:04:12.
- **14:09** — page at 18.4% 5xx.
- **14:20–14:41** — vendor advisory opened and closed; vendor p99 stayed ~130ms.
- **14:52–15:12** — instance restarts; no change.
- **15:31** — rollback `v4.18.3` workers=8.
- **15:38** — mitigated.

## Root Cause

The pool was sized for 8 workers and never re-validated when workers became 32.
That is the cause; the deploy is only the trigger. Establishing evidence:
`service-config.yaml` (`max_connections: 10`, `acquire_timeout_ms: 5000`), the
14:02:11 startup line, the `connection pool exhausted (max=10, waiters=...)`
errors, and `metrics.csv` pinning `db_pool_active` at 10 while wait p99 rose
to 3104. Recovery on rollback without a database change confirms it.

Ruled out: the 14:20 payments-vendor advisory (latency flat, 200s, wrong
timing), search-api TLS (not on the checkout path), ledger-db CPU 72% (symptom).

## Contributing Factors

- Detection waited for 5xx > 5% because no pool-saturation alert exists.
- No CI check that `workers` and `max_connections` are consistent.
- Canary disabled.
- Vendor hypothesis delayed looking at the deploy.

## Corrective Actions

1. Startup or CI assertion: refuse boot when workers exceed pool size.
2. Raise `max_connections` for the intended 32-worker concurrency and load-test
   before re-shipping v4.19.0.
3. Alert on pool acquire wait and pool saturation.
4. Enable canary deploys (`deployment.canary: true`).

## Open Questions

- Payment reconciliation for charges without confirmations is unavailable.
- The reason for choosing 32 workers is not in the evidence.
- Whether sibling services have the same uncoupled pool setting.
""",
)

_add(
    CHECKOUT,
    "09-correct-compact",
    "correct",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

v4.19.0 at 14:02 set workers=32 against `db_pool_max=10`. Requests waited for
connections, hit the 5000ms acquire timeout, and returned 500 until rollback to
v4.18.3 at 15:31. Mitigated 15:38.

## Impact

94 minutes of broken checkout, ~7,000 failed POSTs, peak 38.1% 5xx, p99 latency
pinned at the acquire timeout.

## Timeline

- **14:02** — v4.19.0 workers 8→32; startup `workers=32 db_pool_max=10`.
- **14:04** — first pool-exhausted 500.
- **14:09** — 5xx page 18.4%.
- **14:20** — vendor advisory (later ruled out).
- **14:45** — peak 38.1% / wait p99 3104.
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

Uncoupled pool sizing: 32 workers, 10 connections. Cited: startup line,
`TimeoutError: connection pool exhausted (max=10, ...) after 5000ms`,
`metrics.csv` `db_pool_active=10` and `db_pool_wait_p99_ms` 4→3104,
`service-config.yaml` `max_connections: 10`. Vendor advisory is not causal
(`vendor_payments_p99_ms` 127–134ms; errors 14:04–after 14:41). search-api TLS
and ledger-db CPU 72% are not causes.

## Contributing Factors

No pool-saturation alert; no worker/pool CI check; canary false; vendor
hypothesis held ~20 minutes after flat vendor latency was observed.

## Corrective Actions

1. Derive pool size from worker count or fail startup on mismatch.
2. Re-size pool and load-test before re-releasing 32 workers.
3. Alert on `db_pool_wait_p99_ms` and pool saturation.
4. Turn on canary for checkout-api.

## Open Questions

Customer charge-without-confirm status unknown; why workers=32 unknown; other
services not audited in this evidence.
""",
)

_add(
    CHECKOUT,
    "10-correct-timeline-dense",
    "correct",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

The timeline of INC-2291 is the mechanism: the 14:02 worker-count change
immediately saturates a 10-connection pool, 500s begin at 14:04, a vendor
advisory at 14:20 does not move vendor latency, and rolling back workers at
15:31 restores the pool. Mitigated 15:38.

## Impact

- 14:04 to 15:38: 94 minutes.
- 7,021 `checkout_5xx` vs a 2–3 per-bucket baseline.
- Peak 38.1% at 14:45; ~29.5% across the window.
- `#support` complaints from 14:18.

## Timeline

- **14:02** — `deploys.csv` v4.19.0; log `workers=32 db_pool_max=10`.
- **14:03:58** — `db.pool` wait_ms=812 active=10 max=10 waiters=6.
- **14:04:12** — first `connection pool exhausted (max=10, waiters=41) after 5000ms`.
- **14:05** — 6.1% 5xx, wait p99 1450.
- **14:09** — page 18.4%.
- **14:20** — vendor advisory; on-call adopts it.
- **14:23** — vendor p99 still ~130ms.
- **14:41** — advisory clears; our errors do not.
- **14:45** — 38.1% 5xx, wait p99 3104, waiters=97.
- **14:52** — restarts; no change.
- **15:31** — v4.18.3 workers=8.
- **15:33** — wait_ms=3 active=4.
- **15:38** — mitigated.

## Root Cause

32 workers / 10 connections / 5000ms acquire timeout. The deploy triggered a
latent pool-sizing defect. Evidence: startup line, pool warnings, TimeoutError,
`metrics.csv`, `service-config.yaml`. Not the cause: payments vendor (flat
~130ms, 200s, wrong timing), search-api TLS, ledger-db CPU 72%.

## Contributing Factors

No pool alert; uncoupled config; canary off; vendor hypothesis delayed rollback;
restarts tried before rollback.

## Corrective Actions

1. Startup assertion `workers <= max_connections`.
2. Raise `max_connections` and load-test 32 workers.
3. Alert on pool saturation and acquire wait.
4. Enable canary deploys.
5. Add "what did we deploy?" to the first five minutes of the runbook.

## Open Questions

Reconciliation report missing; reason for 32 workers missing; other services
unexamined; vendor's own 14:20 advisory unexplained (not needed here).
""",
)

_add(
    CHECKOUT,
    "11-subtly-wrong-cause-tls",
    "subtly-wrong-cause",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

On 2026-07-14, `search-api v2.2.9` deployed a TLS bundle at 14:05. Checkout
began returning 500s in the same window. We paged at 14:09, spent time on a
payments-vendor advisory, and recovered when we rolled `checkout-api` back to
v4.18.3 at 15:31. Mitigated 15:38.

## Impact

Checkout degraded 94 minutes. About 7,000 POSTs failed, peak 38.1% 5xx, window
rate about 29.5%. Customers complained from 14:18.

## Timeline

- **14:02** — checkout-api v4.19.0 shipped as a routine worker-count change.
- **14:05** — search-api TLS bundle deploy.
- **14:07:30** — cert-expiry notice on search-api.
- **14:09** — checkout 5xx page at 18.4%.
- **14:20** — payments vendor advisory (red herring).
- **15:31** — checkout rollback; errors collapse.
- **15:38** — mitigated.

## Root Cause

The search-api TLS bundle deploy at 14:05 broke certificate validation on the
path checkout uses to resolve product metadata, so checkout requests stalled
and then 500ed. The 14:07:30 cert-expiry notice is the smoking gun. Rolling
back checkout reduced call volume toward search-api and therefore looked like
it fixed checkout; the underlying TLS issue was coincidentally no longer in
the hot path after traffic fell.

v4.19.0's worker change is unrelated. The vendor advisory is also unrelated:
`vendor_payments_p99_ms` stayed near 130ms.

## Contributing Factors

- No canary on search-api TLS rollouts.
- Checkout has a hard dependency on search-api with no timeout isolation.
- Alerting did not name certificate errors.
- Time spent on the vendor advisory.

## Corrective Actions

1. Roll back the search-api v2.2.9 TLS bundle and re-issue certificates.
2. Add a dedicated cert-expiry page 14 days before expiry.
3. Put a circuit breaker on checkout's search-api client.
4. Require canaries for TLS material deploys.

## Open Questions

- How many checkout 500s were specifically certificate errors? The evidence
  logs show TimeoutError text, which we interpret as a downstream timeout
  while waiting on search-api.
- Was the payments vendor also affected by the same certificate family?
""",
)

_add(
    CHECKOUT,
    "12-subtly-wrong-cause-ledger-cpu",
    "subtly-wrong-cause",
    """
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
""",
)

_add(
    CHECKOUT,
    "13-subtly-wrong-cause-traffic",
    "subtly-wrong-cause",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

A traffic spike beginning just after 14:00 overwhelmed checkout-api. v4.19.0
had raised workers to 32 specifically to absorb more traffic, but the spike
still exceeded what the service could serve. Rollback at 15:31 coincided with
the spike ending. Mitigated 15:38.

## Impact

94 minutes of degradation, ~7,000 failed POSTs, peak 38.1% 5xx.

## Timeline

- **14:00** — request volume begins to climb above the midday baseline.
- **14:02** — v4.19.0 ships, intended to add throughput.
- **14:04** — 500s as the spike exceeds capacity.
- **14:09** — page at 18.4%.
- **14:45** — peak 38.1%.
- **15:31** — rollback; traffic also recedes.
- **15:38** — mitigated.

## Root Cause

An unforecasted traffic spike. `metrics.csv` shows `checkout_requests` in the
1200/5min range, which we read as a surge relative to overnight. The worker
increase was the right idea executed too late. Pool messages are what a
saturated service looks like during a spike.

The vendor advisory at 14:20 is unrelated (latency flat). search-api TLS is
unrelated.

## Contributing Factors

- No load-shed or admission control on `/v1/checkout`.
- Capacity planning did not include a 14:00 promotional event.
- Autoscaling is not enabled.

## Corrective Actions

1. Enable horizontal autoscaling on checkout-api.
2. Add a load-shed 503 when in-flight requests exceed 80% of workers.
3. Require a capacity ticket for any marketing campaign.
4. Keep a warm standby region.

## Open Questions

- Which campaign drove the spike? No marketing ticket is in the evidence.
- Would 64 workers have absorbed it?
""",
)

_add(
    CHECKOUT,
    "14-subtly-wrong-cause-code-bug",
    "subtly-wrong-cause",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

v4.19.0 introduced a connection-leak in the checkout handler. Workers were
raised 8→32 in the same release, which multiplied the leak. We paged at 14:09
and recovered by rolling back to v4.18.3 at 15:31. Mitigated 15:38.

## Impact

94 minutes, ~7,000 failed checkouts, 38.1% peak 5xx, ~29.5% across the window.

## Timeline

- **14:02** — v4.19.0 deploy (worker change plus the leaking handler).
- **14:04** — pool exhausted; first TimeoutError.
- **14:09** — page.
- **14:20** — vendor advisory considered and later dropped (latency ~130ms).
- **14:52** — restarts; leak immediately refilled the pool.
- **15:31** — rollback removes the leak.
- **15:38** — mitigated.

## Root Cause

A connection leak in the v4.19.0 handler. The pool sitting at `active=10
max=10` with a growing waiter count is the signature of connections that are
checked out and never returned. Raising workers from 8 to 32 only made the
leak faster. The 10-connection pool size is not itself the defect; it had
been stable since 2025-11-04.

We rule out the payments vendor (flat p99, 200s). We rule out search-api TLS.

## Contributing Factors

- No integration test that asserts connections return to the pool.
- Restarts were tried before rollback, which cannot clear a leak that
  reproduces on the new code.
- Canary was off, so all six instances leaked at once.

## Corrective Actions

1. Revert the v4.19.0 handler diff and add a regression test that checks
   `db_pool_active` after 1k requests.
2. Add a runtime guard that pages if `db_pool_active` stays at max for 60s.
3. Enable canary deploys.
4. Require a connection-pool soak test on any checkout-api change.

## Open Questions

- Which function leaked? The supplied evidence has no stack of an unreturned
  checkout, only the TimeoutError.
- Was the leak in new worker-pool code or in the payment client?
""",
)

_add(
    CHECKOUT,
    "15-right-cause-useless-actions-generic",
    "right-cause-useless-actions",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

On 2026-07-14, `checkout-api v4.19.0` raised HTTP workers from 8 to 32 while
the per-instance pool stayed at 10. Requests queued, hit the 5000ms acquire
timeout, and returned 500. Rollback to v4.18.3 at 15:31 restored service.
Mitigated 15:38.

## Impact

94 minutes of degraded checkout, ~7,000 failed POSTs, peak 38.1% 5xx, window
rate ~29.5%.

## Timeline

- **14:02** — v4.19.0 `workers=32 db_pool_max=10`.
- **14:04** — first `connection pool exhausted (max=10, ...)` 500.
- **14:09** — page at 18.4%.
- **14:20–14:41** — vendor advisory, latency flat ~130ms, then cleared.
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

32 workers against a pool of 10. Evidence: startup line, TimeoutError text,
`metrics.csv` `db_pool_active` pinned at 10 with wait p99 4→3104,
`service-config.yaml` `max_connections: 10`. Deploy is the trigger; uncoupled
pool sizing is the cause. Vendor advisory, search-api TLS, and ledger-db CPU
72% are not causes.

## Contributing Factors

No pool-saturation alert; no worker/pool CI coupling; canary false; vendor
hypothesis delayed the rollback.

## Corrective Actions

- Improve monitoring and alerting across the platform.
- Increase test coverage for configuration changes.
- Review our deployment process and adopt safer rollout practices.
- Increase awareness of database connection management.
- Schedule a follow-up review with the wider engineering team.

## Open Questions

- Were customers charged without an order confirmation?
- Why was the worker count raised to 32?
- Do other services share this pool-sizing assumption?
""",
)

_add(
    CHECKOUT,
    "16-right-cause-useless-actions-rollback-only",
    "right-cause-useless-actions",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

v4.19.0 raised workers 8→32 against a 10-connection pool. Acquire timeouts at
5000ms produced checkout 500s from 14:04 until the 15:31 rollback. Mitigated
15:38.

## Impact

94 minutes, ~7,021 failed POSTs, peak 38.1% 5xx.

## Timeline

- **14:02** — v4.19.0 workers=32 db_pool_max=10.
- **14:04** — pool exhausted.
- **14:09** — page 18.4%.
- **14:20** — vendor advisory (ruled out: p99 127–134ms).
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

Uncoupled pool sizing: 32 workers, 10 connections, 5000ms acquire timeout.
Startup line, TimeoutError, `db_pool_active=10`, `service-config.yaml`. Vendor
advisory is not the cause.

## Contributing Factors

No pool alert; canary off; vendor hypothesis cost time.

## Corrective Actions

1. Keep checkout-api pinned to v4.18.3 indefinitely.
2. Add a dashboard panel for 5xx so the next incident is easier to watch.
3. Remind on-call to consider rollback sooner.
4. Write a Slack postmortem announcement.

## Open Questions

- Charge-without-confirm status unknown.
- Why workers were raised to 32 is unknown.
""",
)

_add(
    CHECKOUT,
    "17-right-cause-useless-actions-process",
    "right-cause-useless-actions",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

Release v4.19.0 set workers=32 while `max_connections` stayed 10, so checkout
requests timed out waiting for a connection. Rollback at 15:31 fixed it.
Mitigated 15:38.

## Impact

94 minutes, ~7,000 failed checkouts, 38.1% peak 5xx, complaints from 14:18.

## Timeline

- **14:02** — deploy v4.19.0.
- **14:04** — first pool-exhausted 500.
- **14:09** — page.
- **14:20–14:41** — vendor hypothesis, then discarded (flat ~130ms).
- **15:31** — rollback.
- **15:38** — mitigated.

## Root Cause

Worker count and pool size were not coupled. Evidence: `workers=32
db_pool_max=10` startup line, `TimeoutError: connection pool exhausted
(max=10, waiters=...) after 5000ms`, `metrics.csv` wait p99 4→3104. The
vendor is not the cause.

## Contributing Factors

Process gaps around review, canary, and incident comms.

## Corrective Actions

1. Hold a blameless workshop on communication during incidents.
2. Update the values document to emphasize careful reviews.
3. Ask teams to be more mindful when changing configuration.
4. Add INC-2291 to the quarterly reliability newsletter.

## Open Questions

- Reconciliation numbers are unavailable.
- Whether other services have the same assumption is unknown.
""",
)

_add(
    CHECKOUT,
    "18-fabricated-evidence-warroom",
    "fabricated-evidence",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

On 2026-07-14 v4.19.0 raised workers from 8 to 32 against a pool of 10.
Requests hit the 5000ms acquire timeout and returned 500. Mira Chen in the
#inc-2291 war room correlated this with a hidden Grafana board
`checkout-pool-sat` that is not in the evidence pack. Rollback at 15:31.
Mitigated 15:38.

## Impact

94 minutes, 7,021 failed POSTs, peak 38.1% 5xx. Finance Slack (`#rev-ops`)
later said $184,400 of GMV was at risk; that figure is not in `/app/evidence`.

## Timeline

- **14:02** — v4.19.0; startup `workers=32 db_pool_max=10`.
- **14:04** — TimeoutError pool exhausted.
- **14:06** — Mira Chen pages a secondary "pool saturation" board.
- **14:09** — official 5xx page 18.4%.
- **14:20** — vendor advisory; ruled out (p99 ~130ms).
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

32 workers / 10 connections. Startup line, TimeoutError, `metrics.csv`
`db_pool_active=10`, `service-config.yaml`. Vendor advisory is not the cause.
Mira's unpublished `checkout-pool-sat` dashboard is cited as a third signal.

## Contributing Factors

- No pool alert in `alerts.log`.
- Canary false.
- The war-room Zoom recording (not in evidence) shows 14 minutes lost to the
  vendor hypothesis.

## Corrective Actions

1. Couple pool size to worker count or fail startup on mismatch.
2. Alert on pool saturation.
3. Enable canary deploys.
4. Promote Mira's unofficial dashboard into the official pack.

## Open Questions

- Charge-without-confirm status unknown.
- Why workers=32 is unknown.
""",
)

_add(
    CHECKOUT,
    "19-fabricated-evidence-dashboards",
    "fabricated-evidence",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

v4.19.0 at 14:02 set workers=32 against `db_pool_max=10`. Datadog monitor
`checkout.db.pool.saturation` (id 88412) allegedly crossed 95% at 14:03:40;
that monitor does not exist in the supplied `alerts.log` or `metrics.csv`.
Rollback at 15:31. Mitigated 15:38.

## Impact

94 minutes, ~7,000 failed POSTs, peak 38.1% 5xx. A PagerDuty note attributed
to `s.lindqvist` claims 12,400 distinct customers; that count is not in the
evidence.

## Timeline

- **14:02** — v4.19.0 workers=32 db_pool_max=10.
- **14:03:40** — Datadog 88412 (not in evidence) fires.
- **14:04** — TimeoutError in `checkout-api.log`.
- **14:09** — `checkout-api-5xx-rate` pages 18.4%.
- **14:20** — vendor advisory; vendor p99 stays 127–134ms.
- **15:31** — rollback.
- **15:38** — mitigated.

## Root Cause

Uncoupled pool sizing, 32 vs 10. Establishing real evidence: startup line,
TimeoutError, `db_pool_wait_p99_ms` 4→3104. The Datadog monitor is cited as
if it were in `/app/evidence`. Vendor is not the cause.

## Contributing Factors

No pool alert in the real `alerts.log`; canary false.

## Corrective Actions

1. Startup assertion workers <= max_connections.
2. Re-size the pool and load-test.
3. Import Datadog 88412 into the checked-in alert list.
4. Enable canary.

## Open Questions

- Reconciliation report missing.
- Whether Datadog 88412 was ever production-enabled is not answerable from
  the supplied files.
""",
)

_add(
    CHECKOUT,
    "20-fabricated-evidence-tickets",
    "fabricated-evidence",
    """
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

INC-2291 followed JIRA `CAP-4419`, which (this document claims) approved
raising checkout-api workers to 32 without a pool change. The 14:02 deploy
then exhausted the 10-connection pool. Rollback 15:31, mitigated 15:38.

## Impact

94 minutes, 7,021 failed POSTs, peak 38.1% 5xx. CAP-4419 is not in the
evidence directory.

## Timeline

- **13:10** — CAP-4419 marked "ready to ship" by `r.okafor` (not in evidence).
- **14:02** — v4.19.0; log `workers=32 db_pool_max=10`.
- **14:04** — pool exhausted TimeoutError.
- **14:09** — page 18.4%.
- **14:20** — vendor advisory; latency flat.
- **15:31** — rollback v4.18.3.
- **15:38** — mitigated.

## Root Cause

32 workers, 10 connections, 5000ms acquire timeout. Real evidence: startup
line, TimeoutError, `metrics.csv`, `service-config.yaml`. CAP-4419 is treated
as if it were a supplied artifact. Vendor advisory is not the cause.

## Contributing Factors

No pool alert; canary off; change-management ticket not attached to the
incident bundle.

## Corrective Actions

1. Couple pool size to workers.
2. Alert on pool saturation.
3. Require CAP tickets to attach a pool-sizing screenshot.
4. Enable canary.

## Open Questions

- Charge-without-confirm unknown.
- CAP-4419's actual contents cannot be verified from `/app/evidence`.
""",
)

_add(
    CHECKOUT,
    "21-style-only-fluent-executive",
    "style-only-fluent",
    """
# Postmortem: Platform reliability event

## Summary

We experienced a customer-visible reliability event on the commerce path.
Leadership was informed, a war room was convened, and the platform was
returned to a healthy state through a coordinated mitigation. This note
records the narrative for the weekly operating review.

## Impact

A meaningful share of customer journeys failed during the window. Brand
trust, conversion, and support load were all affected. Exact quantities will
be finalized by finance and are not enumerated here.

## Timeline

- 14:09 — leadership notified that a severity event was in progress.
- 14:30 — cross-functional response team assembled.
- 15:00 — mitigation options compared.
- 15:31 — mitigation executed.
- 15:38 — service health restored.

## Root Cause

The event reflects an architectural tension between throughput ambitions and
an implicit resource constraint. The system behaved as designed given the
constraint; the design did not make the constraint visible to the change that
encountered it.

## Contributing Factors

- Organizational silos between application and data tiers.
- A culture that rewards shipping over rehearsal.
- Incomplete executive dashboards for leading indicators.

## Corrective Actions

- Commission a reliability narrative for the board.
- Revisit our north-star metrics for customer trust.
- Invest in a multi-quarter resilience program.
- Celebrate the response team's collaboration.

## Open Questions

- How should we talk about this class of event externally?
- What investment level matches our risk appetite?
""",
)

_add(
    CHECKOUT,
    "22-style-only-fluent-runbook",
    "style-only-fluent",
    """
# Postmortem: Follow-the-runbook incident

## Summary

The on-call engineer followed the standard availability runbook. Alerts
fired, dashboards were checked, a mitigation from the playbook was applied,
and the service recovered. The runbook worked as written.

## Impact

Users saw errors until the playbook mitigation landed. Duration and volume
were within the range the runbook treats as a Sev-2.

## Timeline

- 14:09 — page received; acknowledge within SLA.
- 14:15 — runbook step 1: confirm symptom on the golden dashboard.
- 14:30 — runbook step 2: gather responders.
- 15:00 — runbook step 3: choose mitigation.
- 15:31 — runbook step 4: apply mitigation.
- 15:38 — runbook step 5: declare mitigated.

## Root Cause

Per the runbook, a recent change interacted with an existing limit. The
runbook does not require naming the limit when the mitigation is a rollback.

## Contributing Factors

- Runbook could be shorter.
- New responders needed coaching on the template.
- The template's "root cause" box is easy to fill with generalities.

## Corrective Actions

- Tighten the runbook wording.
- Add a screenshot of the golden dashboard to the template.
- Train the next on-call cohort on the template.
- File a ticket to review the template next quarter.

## Open Questions

- Should Sev-2s require a named mechanism?
- Is the template too flexible?
""",
)

# ---------------------------------------------------------------------------
# retry-storm-backlog
# ---------------------------------------------------------------------------

_add(
    RETRY,
    "07-correct-retry-math",
    "correct",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The SMS gateway returned 503 for six minutes (03:11–03:17). `notify-worker`
retries with `max_attempts: unlimited`, `delay_ms: 1000`, no backoff, no
jitter, on the same 64 slots as first attempts. That policy turned a
six-minute trigger into a backlog that lasted until 07:03. INC-2307.

## Impact

SMS, including verification codes, missing from ~03:15 to 07:03 (~3h 48m).
Peak lag 13,920s. Peak queue 812,400 vs ~2,100. 806,114 messages drained at
07:01. Nobody paged; support escalated at 06:04.

## Timeline

- **02:47** — v2.8.1 log-format deploy; queue stays ~2,100 until 03:11.
- **03:11** — first `503 upstream_unavailable attempt=1`.
- **03:13** — 12,904 messages in retry state.
- **03:17** — gateway recovers; `m-8841207` accepted on attempt=351.
- **03:18** — `0 free worker slots, 64/64 busy`.
- **03:20** — notifications-db CPU 86% (symptom).
- **06:00** — queue-depth email at 812,400.
- **06:04** — INC-2307 opened.
- **06:14–06:31** — v2.8.1 suspected, then diffed out.
- **07:01** — drain + capped exponential backoff with jitter.
- **07:03** — resolved.

## Root Cause

Unbounded, non-backoff, non-isolated retries. Evidence: `worker-config.yaml`
retry block and shared 64-slot pool; startup
`retry.max_attempts=unlimited retry.delay_ms=1000 retry.jitter=none`;
`m-8841207` climbing to attempt=351; `retry_share_pct` 3→95; scheduler `0
free worker slots`. Gateway 503 is the trigger; the policy is the cause
(~38× longer). v2.8.1 is a log-format change and the queue was healthy
02:47–03:11. DB CPU 86% is downstream of retry volume.

## Contributing Factors

Queue alert is email-only at 500,000; no lag/retry/slot alerts; no DLQ;
shared slot pool; 17 minutes on the deploy hypothesis.

## Corrective Actions

1. Commit `max_attempts=6`, exponential backoff, full jitter.
2. Add a dead-letter queue for exhausted messages.
3. Give retries their own concurrency budget.
4. Page on delivery lag >300s.
5. Route `notify-queue-depth` to the pager and lower the threshold.
6. Circuit-break the gateway client.

## Open Questions

- Duplicate deliveries: `idempotency_key: message_id` behaviour is
  undocumented; `m-8841207` was submitted 351 times.
- Were the 806,114 drained messages re-queued or dropped?
- Gateway's own 03:11–03:17 RCA is not in the evidence.
- Distinct customer count unknown.
""",
)

_add(
    RETRY,
    "08-correct-config-led",
    "correct",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

`worker-config.yaml` still has `retry.max_attempts: unlimited`, `delay_ms:
1000`, `backoff: none`, `jitter: none`, and one 64-slot pool for first
attempts and retries. When the gateway returned 503 from 03:11 to 03:17 that
config produced a self-sustaining retry storm. Recovery at 07:03 required a
drain plus a live config change.

## Impact

~3h 48m without SMS. Peak lag 13,920s, peak depth 812,400, 806,114 drained.
Detection via support at 06:04, not a page.

## Timeline

All times UTC 2026-07-22.

- **02:47** — v2.8.1; queue healthy.
- **03:11–03:17** — gateway 503 window (`ticket.md`, logs, `gateway_503`
  only in the 03:20 metrics bucket).
- **03:18** — 64/64 busy, first-attempt queue starved.
- **06:00** — email alert at 812,400.
- **07:01** — drain; `max_attempts=6` backoff=exponential jitter=full.
- **07:03** — resolved.

## Root Cause

The retry policy, not the six-minute outage, explains a 3h52m customer
outage. Evidence: config retry block; startup line; `m-8841207` attempt
cadence; `messages in retry state` 12,904→771,240; `retry_share_pct` to 95.
v2.8.1 is not the cause (log formatting, healthy queue for 24 minutes).
notifications-db CPU 86% is a symptom.

## Contributing Factors

Email-only 500k alert; no lag/slot alerts; `dead_letter_queue: null`; shared
slots.

## Corrective Actions

1. Persist the 07:01 retry bounds in source control.
2. Configure a DLQ with replay.
3. Cap retry concurrency.
4. Page on delivery lag and on worker-slot saturation.
5. Lower and re-route `notify-queue-depth`.

## Open Questions

Duplicates under repeated `message_id`; fate of drained messages; gateway
RCA; customer cardinality.
""",
)

_add(
    RETRY,
    "09-correct-compact",
    "correct",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Six minutes of gateway 503 (03:11–03:17) plus unlimited 1s retries on 64
shared slots produced a self-sustaining backlog until 07:03.

## Impact

SMS out ~03:15–07:03. Lag 13,920s. Queue 812,400. 806,114 drained. No page.

## Timeline

- **02:47** — v2.8.1; queue ~2,100.
- **03:11** — gateway 503 starts.
- **03:17** — gateway recovers; retries do not.
- **03:18** — 0 free slots.
- **06:00** — email at 812,400.
- **06:04** — INC-2307.
- **07:01** — drain + capped backoff.
- **07:03** — resolved.

## Root Cause

Unlimited retries, fixed 1000ms, no jitter, shared 64 slots. Evidence:
`worker-config.yaml`, startup `retry.max_attempts=unlimited`, `m-8841207`
attempt=351, `retry_share_pct` 3→95. Trigger ≠ cause. v2.8.1 ruled out
(format-only, healthy 02:47–03:11). DB CPU 86% is a symptom.

## Contributing Factors

Email alert at 500k; no lag/slot alerts; no DLQ; shared pool.

## Corrective Actions

Bound retries with jitter; DLQ; separate retry budget; page on lag; page
queue-depth at a sane threshold; gateway circuit breaker.

## Open Questions

Duplicates; lost vs delayed; gateway RCA; customer count.
""",
)

_add(
    RETRY,
    "10-correct-slot-focus",
    "correct",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

From 03:18 the scheduler said `0 free worker slots, 64/64 busy, first-attempt
queue not being served`. Those 64 slots were occupied by unlimited 1s retries
started during the 03:11–03:17 gateway 503. The slots never freed themselves.
Drain + backoff at 07:01 ended INC-2307 at 07:03.

## Impact

~3h 48m without SMS/verification codes. Peak lag 13,920s, depth 812,400,
806,114 drained. Support-detected at 06:04.

## Timeline

- **02:47** — v2.8.1 log format; slots not saturated.
- **03:11** — 503s begin.
- **03:13** — 12,904 in retry, holding slots.
- **03:17** — 503s end.
- **03:18** — 64/64 busy.
- **03:20** — db CPU 86% (retry write volume).
- **06:00–06:04** — email then support escalation.
- **06:31** — v2.8.1 diffed, no behaviour change.
- **07:01–07:03** — drain, backoff, recovery.

## Root Cause

Retries and first attempts share 64 slots with no cap, no backoff, no jitter.
Evidence: `workers:` comment in `worker-config.yaml`; startup retry line;
`m-8841207`; `retry_share_pct` 95; repeated `0 free worker slots`. Gateway
fault is the trigger. v2.8.1 and db CPU are not causes.

## Contributing Factors

No slot-saturation alert; email-only depth alert; no DLQ; 17 minutes on the
deploy hypothesis.

## Corrective Actions

1. Cap attempts and add exponential backoff with jitter.
2. Separate retry lane or per-class budget.
3. Page on `worker_slots_busy == 64` and on delivery lag.
4. DLQ.
5. Re-route queue-depth to pager.

## Open Questions

Gateway idempotency on repeated `message_id`; drain fate; gateway RCA.
""",
)

_add(
    RETRY,
    "11-subtly-wrong-cause-db-cpu",
    "subtly-wrong-cause",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

`notifications-db` CPU hit 86% at 03:20 and stayed elevated, slowing
notify-worker enough that the outbound queue grew to 812,400. We drained at
07:01. The gateway's six-minute 503 was a sideshow.

## Impact

SMS missing ~3h 48m. Peak lag 13,920s. Queue 812,400. 806,114 drained. No
page.

## Timeline

- **02:47** — v2.8.1, unrelated.
- **03:11** — gateway 503s start.
- **03:17** — gateway recovers.
- **03:20** — notifications-db CPU warning 86%.
- **06:00** — queue-depth email.
- **06:04** — INC-2307.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

Database CPU saturation. At 86% the worker could not commit delivery state
fast enough, so messages piled up and were retried. The retry counters look
dramatic but they are a consequence of the database not keeping up. 86% is
below the 95% page, which is why nobody came.

v2.8.1 is not the cause (log format only). The gateway window is too short to
explain a four-hour queue.

## Contributing Factors

- DB page threshold 95% is too high.
- No read-replica for worker state writes.
- Queue-depth alert is email-only.

## Corrective Actions

1. Scale notifications-db and lower the CPU page to 70%.
2. Move retry state off the primary.
3. Keep the drain runbook.
4. Lower `notify-queue-depth` to 50,000.

## Open Questions

- Which query burned CPU? Not in the evidence.
- Should we replace notifications-db with Redis?
""",
)

_add(
    RETRY,
    "12-subtly-wrong-cause-schema-index",
    "subtly-wrong-cause",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The notifications-db schema-114 index change from 2026-07-21 22:30 regressed
enqueue writes. Overnight traffic then filled `notifications.outbound` to
812,400. We noticed at 06:04 and drained at 07:01.

## Impact

~3h 48m without SMS, lag 13,920s, queue 812,400, 806,114 drained.

## Timeline

- **2026-07-21 22:30** — schema-114 index.
- **02:47** — v2.8.1 (unrelated log format).
- **03:11–03:17** — gateway 503, coincidental.
- **03:20** — db CPU 86% as the bad index is hammered.
- **06:00** — queue alert email.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

schema-114. The new index made enqueue and retry-state updates expensive, so
workers spent their 64 slots waiting on the database instead of delivering.
Retry amplification is what a slow enqueue path looks like, not an
independent cause.

billing-api v6.1.2 at 04:10 is unrelated. v2.8.1 is log format only.

## Contributing Factors

Index change had no EXPLAIN gate. No canary on schema changes.

## Corrective Actions

1. Revert schema-114.
2. Require EXPLAIN ANALYZE on notification-db DDL.
3. Add a canary replica for index builds.
4. Scale the primary as a precaution.

## Open Questions

- What was the query plan before vs after schema-114? Not in the pack.
- Did billing-api also suffer?
""",
)

_add(
    RETRY,
    "13-subtly-wrong-cause-capacity",
    "subtly-wrong-cause",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

64 worker slots are not enough. After a brief gateway 503 the queue grew
because we lacked instances. Adding retries on those same 64 slots made the
shortage obvious. Drain at 07:01 plus the later backoff change recovered us.

## Impact

SMS out ~3h 48m, lag 13,920s, queue 812,400.

## Timeline

- **02:47** — v2.8.1.
- **03:11** — 503s.
- **03:17** — 503s end; 64 slots remain busy.
- **06:04** — INC-2307.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

Insufficient worker capacity. 64 slots cannot absorb a burst. The config's
unlimited retries are a secondary annoyance; the primary miss is that we did
not autoscale the worker fleet when `worker_slots_busy` hit 64.

v2.8.1 is not the cause. The gateway recovered in six minutes.

## Contributing Factors

No autoscaling. Queue alert too late. No overflow region.

## Corrective Actions

1. Autoscale notify-worker to 256 slots.
2. Add a second region.
3. Keep a standing overflow queue cluster.
4. Lower the email threshold.

## Open Questions

- What slot count would have drained the 03:17 leftover without a human?
- Should we move SMS off this worker entirely?
""",
)

_add(
    RETRY,
    "14-subtly-wrong-cause-gateway-only",
    "subtly-wrong-cause",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The SMS gateway was down. It said 03:11–03:17 but our queue did not recover
until 07:03, so the vendor's window is incomplete. We treated the vendor as
the root cause and drained our side as mitigation.

## Impact

~3h 48m without SMS, 812,400 peak depth, 806,114 drained, no page.

## Timeline

- **02:47** — v2.8.1, healthy afterwards.
- **03:11** — gateway 503s.
- **03:17** — vendor claims recovery.
- **06:04** — customers still have no SMS.
- **06:35** — vendor email restates the six-minute window.
- **07:01** — we drain.
- **07:03** — delivery resumes.

## Root Cause

A sustained gateway outage, not a six-minute blip. Our retry counters and
slot saturation are what a client does when the upstream is still failing,
even if the vendor's status page disagrees. The 07:01 drain worked because it
dropped work the gateway would have continued to reject.

Retry configuration is an implementation detail. v2.8.1 is unrelated.

## Contributing Factors

Single SMS vendor. No secondary gateway. Status-page trust.

## Corrective Actions

1. Replace the SMS gateway vendor.
2. Demand a written RCA for a four-hour outage, not a six-minute one.
3. Add a second provider behind a feature flag.
4. Page when the vendor status page and our delivery lag disagree.

## Open Questions

- Why did the vendor report 03:17 recovery?
- How many customers saw duplicates after we retried?
""",
)

_add(
    RETRY,
    "15-right-cause-useless-actions-generic",
    "right-cause-useless-actions",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Gateway 503s for six minutes (03:11–03:17) plus unlimited 1s retries on 64
shared slots produced a self-sustaining backlog until the 07:01 drain.
Resolved 07:03.

## Impact

SMS out ~03:15–07:03. Lag 13,920s. Queue 812,400. 806,114 drained. No page.

## Timeline

- **02:47** — v2.8.1; queue healthy.
- **03:11–03:17** — gateway 503.
- **03:18** — 64/64 busy.
- **06:00** — email 812,400.
- **06:04** — INC-2307.
- **07:01** — drain + backoff.
- **07:03** — resolved.

## Root Cause

Unlimited retries, fixed 1000ms, no jitter, shared 64 slots. Evidence:
`worker-config.yaml`, startup line, `m-8841207` attempt=351, `retry_share_pct`
3→95. Trigger is the gateway; cause is the policy. v2.8.1 and db CPU 86%
are not causes.

## Contributing Factors

Email-only 500k alert; no lag/slot alerts; no DLQ.

## Corrective Actions

- Improve monitoring.
- Review retry best practices as a team.
- Increase awareness of metastable failures.
- Update the incident newsletter.
- Schedule a workshop.

## Open Questions

Duplicates; drain fate; gateway RCA; customer count.
""",
)

_add(
    RETRY,
    "16-right-cause-useless-actions-drain-only",
    "right-cause-useless-actions",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Unlimited 1s retries on shared slots turned a 03:11–03:17 gateway 503 into a
backlog that lasted until we drained 806,114 messages at 07:01. Resolved
07:03.

## Impact

~3h 48m without SMS, lag 13,920s, queue 812,400.

## Timeline

- **02:47** — v2.8.1 healthy.
- **03:11** — 503s.
- **03:17** — 503s end; retries continue.
- **03:18** — 0 free slots.
- **06:04** — INC-2307.
- **07:01** — drain.
- **07:03** — resolved.

## Root Cause

Retry amplification: unlimited attempts, 1000ms, no backoff, 64 shared slots.
Evidence: config, startup line, attempt counters, `retry_share_pct`. v2.8.1
ruled out.

## Contributing Factors

Late email alert; no DLQ.

## Corrective Actions

1. Keep the drain script in the on-call gist.
2. Add a queue-depth graph to the existing dashboard.
3. Remember to drain sooner next time.
4. Write a status-page template for SMS delays.

## Open Questions

Duplicates; whether drained messages were dropped.
""",
)

_add(
    RETRY,
    "17-right-cause-useless-actions-process",
    "right-cause-useless-actions",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

The retry policy (`unlimited` / 1000ms / no jitter / shared 64 slots) turned
a six-minute gateway fault into a four-hour outage. Drain at 07:01. Resolved
07:03.

## Impact

SMS missing ~3h 48m, 812,400 peak, 806,114 drained, support-detected.

## Timeline

- **02:47** — v2.8.1.
- **03:11–03:17** — gateway 503.
- **03:18** — slots full.
- **06:04** — INC-2307.
- **07:03** — resolved.

## Root Cause

Unbounded non-isolated retries. Evidence: `worker-config.yaml`, startup
`retry.max_attempts=unlimited`, `m-8841207`, `retry_share_pct` 95. Not
v2.8.1. Not db CPU.

## Contributing Factors

Detection and comms process, not just config.

## Corrective Actions

1. Hold a blameless conversation about paging culture.
2. Add INC-2307 to the reliability reading list.
3. Ask people to be more careful with retries.
4. Put "think about amplification" on the design-review checklist as prose
   with no owner or done-when.

## Open Questions

Customer count; gateway RCA.
""",
)

_add(
    RETRY,
    "18-fabricated-evidence-warroom",
    "fabricated-evidence",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Unlimited 1s retries on 64 shared slots amplified a 03:11–03:17 gateway 503.
In the #inc-2307 Zoom, Priya Natarajan pulled a private Honeycomb board
`retry-amp-prod` that is not in `/app/evidence`. Drain at 07:01, resolved
07:03.

## Impact

~3h 48m without SMS, lag 13,920s, queue 812,400. Priya estimated 2.4 million
push+SMS recipients; that number is not in the supplied files.

## Timeline

- **02:47** — v2.8.1; queue ~2,100.
- **03:11** — 503s.
- **03:17** — vendor recovery.
- **03:18** — 64/64 busy.
- **03:22** — Priya's Honeycomb board (not in evidence) shows retry amp.
- **06:04** — INC-2307.
- **07:01–07:03** — drain, resolve.

## Root Cause

Retry policy, not the six-minute outage. Real evidence: config, startup
line, `m-8841207`, `retry_share_pct`. The Honeycomb board is cited as if it
were supplied. v2.8.1 and db CPU 86% are not causes.

## Contributing Factors

Email-only alert; no DLQ; shared slots.

## Corrective Actions

1. Commit capped exponential backoff.
2. DLQ.
3. Separate retry budget.
4. Export Priya's board into the incident bundle.

## Open Questions

Duplicates; drain fate; whether Honeycomb 2.4M is real.
""",
)

_add(
    RETRY,
    "19-fabricated-evidence-vendor-rca",
    "fabricated-evidence",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Our retry policy amplified a six-minute 503. This write-up also quotes a
vendor RCA email from `sre@sms-gateway.example` blaming a BGP flap in
`eu-west-2b` at 03:11. That email is not in `/app/evidence` (only a
confirmation of the window exists). Drain 07:01, resolved 07:03.

## Impact

~3h 48m, lag 13,920s, queue 812,400, 806,114 drained.

## Timeline

- **02:47** — v2.8.1 healthy.
- **03:11–03:17** — 503s.
- **03:18** — 0 free slots.
- **04:02** — vendor RCA email (not in evidence) names BGP.
- **06:04** — INC-2307.
- **07:03** — resolved.

## Root Cause

Unlimited retries / 1000ms / no jitter / shared 64 slots. Evidence: config,
startup line, attempt=351, `retry_share_pct` 95. The BGP story is presented
as fact. v2.8.1 is not the cause.

## Contributing Factors

Email alert; no DLQ.

## Corrective Actions

1. Cap retries with jitter.
2. Circuit-break the gateway.
3. File the BGP email into the evidence pack.
4. Page on delivery lag.

## Open Questions

Duplicates; whether the BGP claim can be verified.
""",
)

_add(
    RETRY,
    "20-fabricated-evidence-metrics",
    "fabricated-evidence",
    """
# Postmortem: INC-2307 — notification delivery backlog

## Summary

Unlimited retries amplified a 03:11–03:17 503. This document cites
`retry_in_flight{le="64"}` on Prometheus job `notify-slo` crossing 1.0 at
03:12:08 — a metric and job that do not appear in `metrics.csv`. Resolved
07:03 after the drain.

## Impact

~3h 48m, 13,920s lag, 812,400 depth. A claimed SLO burn of 14.2x is not in
the supplied metrics.

## Timeline

- **02:47** — v2.8.1.
- **03:11** — 503s.
- **03:12:08** — invented Prometheus series fires.
- **03:17** — 503s end.
- **03:18** — 64/64 busy (real log line).
- **06:04** — INC-2307.
- **07:03** — resolved.

## Root Cause

Retry amplification. Real evidence: `worker-config.yaml`, startup line,
`m-8841207`, `retry_share_pct`. The Prometheus series is fabricated. v2.8.1
and db CPU are not causes.

## Contributing Factors

Email-only 500k alert; no DLQ; shared slots.

## Corrective Actions

1. Bound retries.
2. DLQ.
3. Page on delivery lag.
4. Check the invented `notify-slo` burn rate into the next dashboard.

## Open Questions

Duplicates; drain fate; customer count.
""",
)

_add(
    RETRY,
    "21-style-only-fluent-executive",
    "style-only-fluent",
    """
# Postmortem: Messaging reliability event

## Summary

Customers experienced a prolonged gap in outbound messaging. The response
organization convened, a mitigation was applied, and delivery resumed. This
note is for the staff meeting, not an engineering design review.

## Impact

Messages were late or missing for several hours. Support volume rose.
Financial impact will be estimated separately.

## Timeline

- 03:11 — upstream partner reported trouble.
- 06:04 — support raised the customer impact.
- 07:01 — mitigation applied.
- 07:03 — service restored.

## Root Cause

A partner disruption interacted with our architecture in a way that prevented
self-recovery. The details are less important than the customer promise we
missed.

## Contributing Factors

- Single-vendor concentration.
- Detection that waited on customers.
- Escalation paths that assume a page will fire.

## Corrective Actions

- Brief the exec team on vendor concentration.
- Fund a multi-quarter messaging resilience theme.
- Improve our customer-apology template.
- Revisit SLAs at the next QBR.

## Open Questions

- How should we describe this externally?
- What spend is justified to avoid a repeat?
""",
)

_add(
    RETRY,
    "22-style-only-fluent-runbook",
    "style-only-fluent",
    """
# Postmortem: Queue-backlog runbook execution

## Summary

On-call executed the queue-backlog runbook. The queue was drained, a config
knob was changed, and the service recovered. The runbook completed.

## Impact

Customers waited on messages until the runbook finished. Exact counts are
left to the metrics appendix, which this template does not require.

## Timeline

- 06:04 — human detection (runbook assumes a page that did not happen).
- 06:14 — runbook step: inspect recent deploys.
- 07:01 — runbook step: drain.
- 07:03 — runbook step: declare resolved.

## Root Cause

Per template: a backlog formed and did not drain on its own. The template's
root-cause box is satisfied by naming "queue backup."

## Contributing Factors

- Runbook assumes a page.
- Drain step has no preventive twin.
- Template allows a one-sentence cause.

## Corrective Actions

- Clarify the runbook's detection step.
- Add a checkbox for "consider retries" without saying what to change.
- Train the next rotation on the template.
- File a ticket to review the template.

## Open Questions

- Should the template require a mechanism?
- Who owns runbook edits?
""",
)

