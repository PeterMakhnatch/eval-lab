# Verifier Notes: R2 Review Load Simulation

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-B-LOOP/report.md`  
**Verifier report SHA256[:12]:** `af6aba70ddae`  
**Status:** Load simulations and throttling policy comparisons CONFIRMED; cost-per-thousand-runs framing QUALIFIED

## Key Notes and Guidance

| Topic | Finding | Status | What Reader Should Use Instead |
|---|---|---|---|
| Unthrottled Review Load | Without throttling, 60.89% of admitted DEV runs generate review cards (~330 cards/day at 541.5 runs/day arrival rate), requiring 11.0 to 16.5 hours of human judging per day. | CONFIRMED | Unthrottled deployment is operationally infeasible; throttling is strictly mandatory. |
| Recommended Policy (Policy C: Strict Budget Cap) | Budget of $K=30$ cards/day caps human review to ~1.0–1.5 hours/day while capturing 23.3 true positives per day at 77.7% precision. | CONFIRMED | Use Policy C ($K=30$ cards/day) for sustainable production operation. |
| Cost per 1,000 Runs Framing | Early B1/B2 statements quoted $2.27 / 1,000 runs. That rate accounted strictly for L0-triggered reviews. When cascade escalation and higher card volumes are evaluated, total cost scales with daily card budget. | QUALIFIED | Use simulation receipts in `receipts/` for end-to-end cost modeling under varying card budgets. |
