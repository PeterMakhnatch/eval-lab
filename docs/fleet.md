---
status: living
audience:
  - builder
  - operator
---

# Retired Fleet supervisor

The Fleet supervisor, its source service template, and `evallab fleet` command are retired.
This change does not stop workers, unload installed services, or alter their state.
Existing lane databases, event histories, receipts, and pinned releases remain unchanged.

Use `evallab status` for native operator state.
Use [ship](ship.md) for exact-head CI, the registered path gates, and protected delivery.
Fleet no longer owns delivery or worker supervision in Eval Lab.
The source before retirement is in the `pre-deslop-2026-11-10` Git tag.
