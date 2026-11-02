# MiMo Gold v1 Certification Summary

**Generated:** 2026-11-02T10:08:29.815963+00:00  
**Universe Size:** 32 strictly-DEV packages  
**Total Settled Spend:** $0.0737 USD  
**Total Cells Executed:** 56  

---

## 1. Key Metrics

| Metric | Count / Cohort | Point Estimate | 95% Wilson Interval |
| :--- | :--- | :--- | :--- |
| **Gold Structural** | 0 / 32 | 0.0% | 0.0-10.7% |
| **Gold Held-Out** | 0 / 32 | 0.0% | 0.0-10.7% |
| **Demonstrated Weak Graders** | 0 / 18 | 0.0% | 0.0-17.6% |
| **Mean Unseen EVAL Kill Rate** | — | 0.0% | — |

---

## 2. Failure Mode Taxonomy

| Reason Code | Count | Description |
| :--- | :--- | :--- |
| `ref_failed_no_repair` | 32 | Ref failed no repair |

---

## 3. Operational Bounds & Overrun Guarantee

- **Modal Reservation:** Sized on `min(240 s, 300 s)` = $0.03966 per cell.
- **Explicit Timeout:** `timeout_seconds = 300` on every cell.
- **Worst-Case In-Flight Overrun:** $300 \times \$0.00016525 - \$0.03966 = \$0.0099$ per cell.
- **Global Concurrency:** 6 container slots in flight $\implies$ maximum possible overrun beyond cap is $6 \times \$0.0099 = \$0.059$.
- **Measured Cap Enforcement:** Verified zero cell double payment under resume or restart.
