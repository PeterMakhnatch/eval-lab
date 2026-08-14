<!-- calibration-variant: subtly-wrong-cause -->
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
