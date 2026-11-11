---
status: living
audience:
  - operator
  - builder
---

# One causal watcher core

The optional Live Supervisor product retires on 2026-11-10.
Its model reader, service scanners, fleet scanner, report writer, alarm router, and separate replay tool are removed.
The `live-supervisor` command and its repository launchd templates are removed.
This source change does not unload installed services or change pinned releases.

## Retained paths

- [`evallab watch`](../README.md) reads live trial files and writes alerts, status, and a board.
- `auto_watch` attaches the watcher to model runs and preserves alert acknowledgements and dispatch protection.
- `harbor_watch_hooks` captures lifecycle events and optional bounded log tails. It never cancels a trial.
- The [causal monitor](../src/evallab/production/monitor_worker.py) retains prefix isolation, source-event citations, capture coverage, budgets, and allowlists.
- [Run Health](run-health.md) retains native error outcomes and observed-step timing.
- [Line watch](line.md) retains finished-run scheduling and watchdog checks.

The `stalled` alert measures file, proxy, and hook inactivity at 600 seconds.
Run Health's `silence` trigger measures observed-step silence at 2,700 seconds.
A fresh file write does not establish a new observed step. These clocks remain separate.
The removed supervisor's 900-second activity clock does not replace either clock.

## Evidence and safety

`live_watch.integrity_alerts` and `tripwires.scan_prefix` remain the native integrity replay path.
Replay supplies only the visible prefix. Alerts retain original source-step citations.
The causal monitor retains its independent streaming contract and post-run artifact isolation.
Trajectory-only replay cannot invent file-access coverage or a pre-agent baseline.

The watcher has no stop actuator or stop command. Alerts do not authorize a stop.
Existing stop requests, stop records, queues, databases, findings, and frozen receipts remain untouched.
Native result readers still distinguish historical `WatchStopCancelledError` outcomes.
Historical supervisor experiments remain historical evidence, not supported runtime entry points.

## Services for later retirement

The operator must separately retire these installed services:

- `com.petermakhnatch.evallab.live-supervisor`
- `com.petermakhnatch.evallab.live-supervisor-alarms`
- `com.petermakhnatch.evallab.live-supervisor-fleet-report`

Keep finished-run Line services and native results-viewer services.
