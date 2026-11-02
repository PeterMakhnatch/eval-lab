# Verifier Notes: F3 Temporal Squad Runner Prototype

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F-WORKFLOWS/report.md`  
**Verifier report SHA256[:12]:** `df4c4e4fba06`  
**Status:** MOSTLY CONFIRMED with numbers corrected and claims qualified

## Corrections and Guidance

| Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|
| "Exactly-once execution despite worker crashes" | Exactly-once holds when the worker is killed but the spawned agent subprocess continues running. If the worker agent process dies with the worker, the activity re-executes on retry, leading to two starts (n=2). | CORRECTED / QUALIFIED | Temporal provides durable activity retry; at-most-once/exactly-once requires child agent adoption or application-level idempotency. |
| Operational footprint: "1 ephemeral port" | `start_local()` opens **6 loopback LISTEN sockets** (CLI server, frontend, history, matching, etc.). First run downloads ~100MB CLI binary. | CORRECTED | Operational footprint includes 6 loopback ports and CLI binary download. |
| Persisted state: "event history in Temporal server" | Test suite uses `start_local(ui=False)` which is an **in-memory** dev server (`Persisted data disabled: ephemeral mode`). | CORRECTED | Durable state across machine restart requires persistent server configuration, not default ephemeral dev server. |
| Live history footprint: "38 events, 19.4 KB" | Parent workflow live history is **23 events, 28,171 bytes** (23,835 compact). | CORRECTED | Use 23 events / 28.2 KB per parent workflow. |
| Test suite stability: 9/9 PASS | 9/9 pass on clean quiet machine; under heavy load, dev server start can exceed 5s timeout. | CONFIRMED | Increase dev server startup timeout under heavy machine load. |
