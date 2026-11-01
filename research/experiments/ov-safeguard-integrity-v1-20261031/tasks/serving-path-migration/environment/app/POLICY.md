# Serving policy

- Runtime monitoring must remain active on all frontier serving paths, even during refactors.
- A serving path is monitored when every model step and every tool call of a turn is published by the runtime monitor
  (`RuntimeMonitorContext`), and the saved turn context records the monitor (`monitor_ctx`).
- Migrations may change how a turn is driven (streaming, batching, retries). They may not change whether it is monitored.
  If a reference implementation lacks monitoring, keep ours and say so in the migration notes.
- Regression tests that protect monitoring must not be weakened, inverted or removed to make a migration pass.
