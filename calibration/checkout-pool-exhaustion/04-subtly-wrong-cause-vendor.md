<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

On 2026-07-14 our payments vendor suffered a latency degradation that
propagated into `checkout-api`, causing `POST /v1/checkout` to return 500
errors for our customers. The vendor published an elevated-latency advisory at
14:20 UTC. We paged at 14:09, tracked the vendor incident, and restored service
by rolling back to `v4.18.3` at 15:31 to reduce load against the vendor. The
incident was mitigated at 15:38.

## Impact

Checkout was degraded for approximately 94 minutes. Roughly 7,000 checkout
requests returned 500 errors, with a peak failure rate of 38.1% in the 14:45
window. Customers reported failures in the support channel from 14:18 onward.
Overall failure rate across the incident window was about 29.5%.

## Timeline

- **14:02** — `checkout-api v4.19.0` deployed as part of the normal release
  train.
- **14:09** — `checkout-api-5xx-rate` paged at 18.4%.
- **14:20** — Payments vendor publishes an "elevated latency" advisory, which
  aligns with the onset of our errors.
- **14:21** — We open a ticket with the vendor.
- **14:41** — Vendor advisory clears, but our error rate has not yet recovered
  because of queued work built up during the vendor degradation.
- **14:52** — Instances restarted to clear the backlog of stuck vendor calls.
- **15:31** — Rollback to `v4.18.3` reduces concurrency against the vendor.
- **15:38** — Error rate returns to baseline, incident mitigated.

## Root Cause

The root cause was a latency degradation at our payments vendor. During the
degraded period, calls to the vendor's `/charge` endpoint took substantially
longer to complete than normal. Because `checkout-api` holds resources for the
duration of an outbound payment call, slow vendor responses caused work to
accumulate inside the service faster than it could be drained. Once that
backlog formed, requests began exceeding our internal request timeout and were
returned to callers as 500 errors.

The vendor advisory at 14:20 confirms that the vendor was in a degraded state
during the incident window. The correlation between the vendor's advisory and
our own error curve is the strongest signal we have. Our own release,
`v4.19.0`, was a routine change and was unrelated to the failure; the rollback
helped only because it reduced the concurrency we were directing at an already
struggling vendor.

## Contributing Factors

- Our dependency on a single payments vendor with no fallback provider.
- Vendor call timeouts (`timeout_ms: 3000`) that are too generous, allowing
  slow calls to occupy resources for too long.
- No circuit breaker around the vendor integration.
- Alerting that did not distinguish vendor-induced failures from our own.

## Corrective Actions

1. Open a formal reliability review with the payments vendor and require a
   root-cause report for their 14:20 advisory, with a target response of ten
   business days.
2. Evaluate a secondary payments provider so that a single vendor degradation
   cannot take checkout down. Done when a failover provider is integrated
   behind a feature flag.
3. Add a circuit breaker around the vendor client that trips after a sustained
   error or latency threshold and sheds load rather than queueing it.
4. Reduce the vendor call timeout from 3000ms to 1500ms so slow vendor calls
   release resources sooner.
5. Add a dedicated alert on vendor call latency so the next vendor degradation
   pages us directly instead of surfacing as a generic 5xx alert.

## Open Questions

- What was the underlying cause of the payments vendor's degradation? We have
  requested a report and have not received one.
- Were any customers charged by the vendor without receiving an order
  confirmation? The payment reconciliation report was not available.
- Would a shorter vendor timeout alone have prevented customer-visible errors,
  or is a circuit breaker required?
