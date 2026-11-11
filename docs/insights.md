---
status: living
audience:
  - builder
  - operator
---

# Retired Insights engine

The Insights engine, analyst orchestration, claim dashboard, and `evallab insights` command are retired.
The source installer and service template are removed. Installed services are not changed.

Use [cached DEV scoring](score.md) for frozen measurements and explicit missingness.
Use native run reports and the results viewer for run evidence.
Keep existing Insights state at `~/.local/state/evallab-insights`.
This change does not modify claim ledgers, receipts, or pinned releases.
The source before retirement is in the `pre-deslop-2026-11-10` Git tag.
