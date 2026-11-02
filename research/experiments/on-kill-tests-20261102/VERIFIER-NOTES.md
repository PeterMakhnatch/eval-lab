# Verifier Notes: F9 Five Kill Tests Cross-Engine Evaluation

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F2-FINAL/report.md`  
**Verifier report SHA256[:12]:** `c43dd95c9f46`  
**Status:** Table cells CORRECTED / UNSUPPORTED; **SUPERSEDED** by F11 (`research/experiments/on-fair-killtests-20261102`)

## Superseded Notice
**Notice to Readers:** This entire folder and its scorecard matrix are superseded by **F11 Fair Kill Tests** (`research/experiments/on-fair-killtests-20261102`). Do not rely on F9's scorecard cells.

## Key Corrections

| Cell / Claim | Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| KT4 Temporal & DBOS | Claimed to fail loudly on live code mutation | Neither test was executed: Temporal KT4 returned a hard-coded pre-written string; DBOS KT4 crashed on an unrecognized CLI flag (`--scratch-dir`). | UNSUPPORTED | Use F11 KT4 execution results. |
| KT3 Timer Parking | Claimed "DBOS is the only engine with native timer parking" | Measures application wrapper code rather than engine primitive. Temporal passes KT3 with standard retry/backoff configuration. | CORRECTED | Both Temporal and DBOS support timer-based backoff. |
| KT1 Crash Windows | Compared across mismatched kill timings | Kill windows were not calibrated to equal probability across item runtimes. | CORRECTED | Use F11 matched-event kill protocol. |
| `all_killtests_summary.json` | Claimed to contain full 4x5 matrix | Receipt file contained only a single cell (Temporal KT3). Actual manifest SHA256 is `f43f2054f93c` (not `e7428f58b0f8`). | CORRECTED | Consult F11 `all_killtests_summary.json`. |
