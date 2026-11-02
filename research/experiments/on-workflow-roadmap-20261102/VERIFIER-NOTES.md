# Verifier Notes: F6 / F10 Workflow Roadmap & Decision Memo

**Verifier reports:**
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F-WORKFLOWS/report.md` (SHA256[:12]: `df4c4e4fba06`)
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F2-FINAL/report.md` (SHA256[:12]: `c43dd95c9f46`)
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F3-FAIR/report.md` (SHA256[:12]: `dfd8c7a8b5c0`)

**Status:** Claims in F6 and F10 CORRECTED; F11 Fair Kill-Tests (`on-fair-killtests-20261102`) supersedes F9 and F10 kill test matrices.

## Key Corrections and Context

| Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|
| Top-5 workflows cover "51 of 70 = 72.9% (61.4-82.0)" | F6's own `evidence_summary.json` gives 46 of 70 = 65.7% (54.0-75.7%) for the top-5 friction items. | CORRECTED | Use 46/70 = 65.7% (54.0-75.7%). |
| Temporal scorecard "5/5 PASS" in F6 | In F6, no Temporal kill-tests had yet been run; claims were inferred rather than measured. | UNSUPPORTED | Reference F11 (`on-fair-killtests-20261102`) for measured kill-test results. |
| Temporal operational ports "7233, 8233" | F3/F4 prototype workflows run via embedded `start_local(ui=False)` on ephemeral ports. | CORRECTED | Default dev server uses ephemeral ports; persistent server uses 7233/8233. |
| DBOS "only interrupted tasks re-ran" / "DBOS would prevent orphan double-execution" | In F9 and confirmed in F11, DBOS does not terminate running child processes upon worker death, leading to ~34.7s of live concurrent orphan overlap. | REFUTED / CORRECTED | DBOS alone does not prevent orphan overlap; requires child process management. |
| F9 / F10 engine scorecard matrix | 5 cells in F9 were unsupported (e.g. KT4 strings); F11 reran with event-driven harnesses. | SUPERSEDED | Use F11 (`on-fair-killtests-20261102`) as the authoritative kill-test matrix. |
| Spend gate PR reference `#1737` | `#1737` was HAR-254; spend gate PR is `#1738`. | CORRECTED | Refer to PR `#1738`. |
