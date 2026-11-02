# Verifier Notes: F4 Merge Train Workflow Prototype

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F-WORKFLOWS/report.md`  
**Verifier report SHA256[:12]:** `df4c4e4fba06`  
**Status:** Tests CONFIRMED (10/10 with mock `gh`); crash recovery during rebase activity REFUTED

## Corrections and Guidance

| Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|
| "Worker crash recovers automatically: in-flight activities re-attach or retry" (Scenario 6 PASS) | When the worker process is killed (`kill -9`) during the active rebase activity, the train hangs waiting for the 15-minute activity heartbeat timeout. On timeout expiration, the workflow fails completely with the PR left unmerged. | REFUTED | In-flight git mutation activities (rebase/merge) require short activity heartbeat timeouts and idempotent git cleanup/rollback handlers before retrying. |
| Test suite results: 10/10 PASS | 10/10 test scenarios pass against the provided mock harness (`fake_gh.py` and local bare git repo). | CONFIRMED | Validated under synthetic fixtures; real GitHub CLI requires error/rate-limit handling. |
