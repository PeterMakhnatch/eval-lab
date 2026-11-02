# Verifier Notes: F5 CertifyGold Durable Temporal Workflow Prototype

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F-WORKFLOWS/report.md`  
**Verifier report SHA256[:12]:** `df4c4e4fba06`  
**Status:** Architecture and test suite CONFIRMED; concurrency and receipt hashes CORRECTED

## Corrections and Guidance

| Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|
| "0 duplicate executions despite kill -9" | The test metric checked unique result keys; interrupted attempts that were killed during execution were not tracked by an attempt counter. | UNSUPPORTED | Measure execution attempts directly via logged activity start timestamps/attempt IDs. |
| "Peak Modal concurrency = 2 (limit 2), synth 1" | Independent concurrency probe observed peak modal concurrency was 1, not 2. | CORRECTED | Under the test schedule, modal executions ran sequentially (peak 1). |
| Manifest SHA: `3e82a0b16a4e` | Actual `manifest.json` SHA256 is `9bf3f9de145a`. | CORRECTED | Use `9bf3f9de145a`. |
| History bounds via `continue_as_new` | Verified: `continue_as_new` executes after configured batch thresholds to bound event history. | CONFIRMED | Continue-as-new pattern is functional. |
