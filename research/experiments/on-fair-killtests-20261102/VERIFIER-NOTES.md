# Verifier Notes: F11 Fair Kill-Tests Evaluation

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-F3-FAIR/report.md`  
**Verifier report SHA256[:12]:** `dfd8c7a8b5c0`  
**Status:** 19 of 35 scorecard cells CONFIRMED; 16 cells CORRECTED or UNSUPPORTED

## Key Corrections and Guidance

| Cell / Claim | Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Gate Verdict: "PASS, all 7 tests executed on 5 engines" | All 7 tests executed, but 16 of 35 cells do not measure what the table claims. | NOT CONFIRMED (19/35 Confirmed) | Interpret scorecard cells with the specific caveats below. |
| KT7 Worktree Race: "Baseline Hardened PASS (fcntl.flock, 12/12)" | `test_kt7_worktree_race.py` tested plain `git worktree add` with and without a test-harness lock; neither `ds_squad.py` nor `ds_squad_v2.py` implements a `flock` (both use a retry loop). | UNSUPPORTED / NOT AN ENGINE TEST | Worktree flock is a proposed harness wrapper, not an existing runner feature in `ds_squad_v2.py`. |
| Hardened Baseline "5/5 safe" Crash Survival | Holds when child agent processes outlive the killed runner (confirmed in R8 with real `omp` workers, 10/10 adoption). When agents die with the runner, `ds_squad_v2.py` marks the in-flight item failed and skips dependents (0/2), whereas Temporal completes 2/2. | QUALIFIED | Baseline hardened adoption requires worker agents to outlive the runner process. |
| Dagu KT1 Max Overlap: "36.78 s" | Calculated statically from step duration; measured live process overlap was **0 s**. | CORRECTED | Dagu retry waits for previous attempt slot; live overlap is 0. |
| KT2 DBOS (0/3) | Attributed to SQLite restart; however, the adapter lacked an approval gate. | UNSUPPORTED | Test adapter defect rather than engine state limitation. |
| KT2 Dagu (0/3) | Stated human.task needed scheduler; receipt shows `prep_starts = 0, deploy_starts = 0` (first step never ran). | UNSUPPORTED | Test launch failed before reaching approval step. |
| KT4 Temporal, DBOS, Dagu | Live code change claims | Temporal tested timeout stall; DBOS script failed on rejected argument `--scratch-dir`; Dagu returned error on already-running socket. | UNSUPPORTED | KT4 code mutation resilience was not cleanly exercised on these three engines. |
| DBOS KT1 Overlap | ~34.7s live orphan PID overlap | CONFIRMED (reproduced with live PID sampling). | CONFIRMED | DBOS step journaling does not terminate running child processes. |
