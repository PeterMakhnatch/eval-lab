# Verifier Notes: A4 MiMo Gold v1 Batch Certification

**Verifier reports:**
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-A-GOLD/report.md` (SHA256[:12]: `eef34a3dad2f`)
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/A4B-FILTERED-GOLD/report.md` (SHA256[:12]: `0b2a4db7d431`)

**Status:** A4 numbers are **HARNESS ARTIFACTS**; completely **SUPERSEDED** by A4B Gold v1.1

## Critical Notice
The headline finding of Gold v1 ("0/32 packages certified, all rejected by false-reject guard") was a **harness defect**, NOT a synthesis failure. Do not cite 0/32 or synthesis capability collapse. Refer to **A4B-FILTERED-GOLD** (`~/Developer/eval-lab-results/2026-11-02/A4B/`) for Gold v1.1 numbers.

## Key Corrections

| Claim / Metric | A4 Reported Value | Verified Finding | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Gold Certification Rate | 0/32 certified (0.0%, 0.0–10.7%) | Pipeline bug: A4 evaluated contract tests with bare `python3` in `/testbed` rather than the task environment (pyenv/venv); 4 tasks never ran because the driver hardcoded `cd /testbed`; repair re-used cached output. | REFUTED (Harness Artifact) | A4B fixed the execution environment: **26/32 tasks (81.2%, 64.7–91.1%)** produced accepted tests; 8/32 are `gold_structural` and 1/32 is `gold_heldout`. |
| Mean Unseen-EVAL Kill Rate | 0.0 | No EVAL cells were executed (`eval_total = 0` for all 32 tasks). | NOT MEASURED | In A4B, pooled unseen-EVAL kill rate is **12/45 = 26.7% (16.0–41.0%)**. |
| Demonstrated Weak Graders | 0/18 (0.0%, 0.0–17.6%) | Zero A2 survivor mutants were exercised because the reference check halted runs beforehand. | NOT MEASURED | Weak graders remain demonstrated by A2 (18/28 = 64.3%). |
| Failure Taxonomy: `ref_failed_no_repair` | 32/32 failures | 15/28 failed at import time under bare python3; 4 tasks failed to launch entirely. | REFUTED | Environment path mismatch accounted for reference test execution failures. |
