# Verifier Notes: F8 Dagu Trial

**Verifier reports:**
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F2-FINAL/report.md` (SHA256[:12]: `c43dd95c9f46`)
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F3-FAIR/report.md` (SHA256[:12]: `dfd8c7a8b5c0`)

**Status:** Fixed DAG capabilities CONFIRMED; dynamic crash recovery and approval claims CORRECTED

## Key Corrections and Guidance

| Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|
| KT1 Live PID overlap: "up to 36.78 s overlap" | The 36.78s was an assumed calculation from nominal step lengths, not measured. Independent verifier probe with live PID monitoring measured **0 s overlap**. | CORRECTED | Measured overlap under Dagu retry is 0; Dagu waits for prior execution slot to clear. |
| KT1 Automatic recovery: "no manual step" | The harness polled `dagu status` and itself invoked `dagu retry`; recovery was not autonomous by the Dagu runner process. | CORRECTED | Dagu CLI requires external watchdog or cron trigger to issue retries on crashed DAGs. |
| KT2 Approval across restart: "0/3 because human.task needs a scheduler" | In receipts, `prep_starts = 0, deploy_starts = 0`; the test run never launched the initial step. | CORRECTED | KT2 was not executed to the approval gate due to launch misconfiguration. |
| Single-binary operational model | CONFIRMED: Dagu runs as a standalone Go binary without database or container requirements. | CONFIRMED | Validated as lightweight local CLI binary. |
