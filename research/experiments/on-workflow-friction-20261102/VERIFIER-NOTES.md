# Verifier Notes: F12 Workflow Friction Synthesis

**Verifier and review context:** Coordinator overnight review; cross-check with VF-F3-FAIR (`dfd8c7a8b5c0`)  
**Status:** Incident and review blocker counts CONFIRMED; time-loss numbers and flock claims CORRECTED

## Key Corrections and Guidance

| Claim / Metric | Original Status | Verified Value | What Reader Should Use Instead |
|---|---|---|---|
| Incident counts & Review blocker counts | Script-derived from bridge receipts | CONFIRMED: 70 total friction incidents, 16 review blockers, and taxonomy clustering reproduce from input receipt logs. | Counts are reliable empirical measurements from tonight's runs. |
| Per-incident "time lost" and "hours saved per night" | Presented alongside measured counts | Author's rough qualitative estimates, **NOT measured from receipts**. | Treat all "time lost" (minutes) and "hours saved per night" as author conjectures/estimates, not measured empirical data. |
| Worktree `fcntl.flock` protection | Cited as shipped/evaluated in squad runner | Neither `ds_squad.py` nor `ds_squad_v2.py` implements `fcntl.flock` (both use retry loops). Flock was only implemented inside the KT7 test harness itself. | Worktree flock is a proposed intervention, not an implemented runner mechanism. |
