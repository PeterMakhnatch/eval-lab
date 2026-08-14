<!-- calibration-variant: subtly-wrong-cause -->
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
