# Verifier Notes: B2 Replay Proof R2

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-B-LOOP/report.md`  
**Verifier report SHA256[:12]:** `af6aba70ddae`  
**Status:** Crash recovery CONFIRMED; queue presentation order and throughput numbers QUALIFIED

## Key Notes
- **Crash Recovery:** Confirmed across 5 fresh `kill -9` trials on 20 Terminal Wrench DEV runs; zero duplicate serves, zero lost requests, identical queue entries.
- **Queue Presentation:** Queue order in Judge Station is driven by deterministic shuffle seed, not priority weighting.
- **Cost per 1,000 runs:** Projections reflect L0 trigger costs; see `on-review-load-20261102` for comprehensive cascade and throttling simulations.
