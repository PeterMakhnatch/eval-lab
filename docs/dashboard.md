---
status: living
audience:
  - operator
  - analyst
---

# Retired research dashboard

The Streamlit research dashboard and `evallab dashboard` command are retired.
The factory dashboard and the legacy production replay UI are also retired.

Use `evallab status` for the native operator snapshot.
Use `evallab results-viewer --help` for published run inspection.
Use `evallab judge --help` for the blind native review queue.
Use [cached DEV scoring](score.md) for frozen measurements.

Existing queues, databases, receipts, results, and installed releases remain unchanged.
This source change does not unload installed services.
The source before retirement is in the `pre-deslop-2026-11-10` Git tag.
