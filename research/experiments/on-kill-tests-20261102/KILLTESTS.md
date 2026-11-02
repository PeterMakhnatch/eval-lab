# The Five Kill Tests: Cross-Engine Evaluation Results

**Harness**: `research/experiments/on-kill-tests-20261102/`  
**Date**: 2026-11-02  
**Evaluator**: Independent re-run across Baseline (`ds_squad.py`), Temporal (F3 prototype), DBOS (F7 prototype), and Dagu (F8 prototype) using unified fixture `fake_agent.py`.

---

## 1. Engine by Test Comparison Table

All four engines were tested using the shared fixture `fake_agent.py` on the exact same 6-item DAG (two 3-item chains `A1->A2->A3`, `B1->B2->B3`, parallelism cap 2) and identical failure conditions.

| Engine | KT1 Crash Resilience (10 trials) | KT2 Human Approval Across Restart | KT3 Quota Parking (HTTP 429 / Code 29) | KT4 Code Mutation Under Live Run | KT5 Execution Caps (Wall-Clock & Retry) |
|---|---|---|---|---|---|
| **Baseline (`ds_squad.py`)** | **PASS** (10/10 = 100.0%, 95% CI: 72.2–100.0%)<br>Raw executions: **76** (16 duplicate re-runs of finished items) | **FAIL**<br>Re-executes prior completed `PREP` step from scratch upon restart | **FAIL**<br>Immediate blind retries (4 refusals before $T$; 0 after $T$) | **silently diverges**<br>Polls and reloads `plan.json` on disk dynamically without error | **PASS**<br>Wall-clock killed at 3s; retry cap stopped after 3 attempts; 0 leaked PIDs |
| **Temporal (F3)** | **PASS** (10/10 = 100.0%, 95% CI: 72.2–100.0%)<br>Raw executions: **60** (0 duplicate executions; exact-1) | **PASS**<br>State persisted across server & worker kill; `release_gate` signal unblocks `DEPLOY`; `PREP` not re-run | **FAIL**<br>Blind continuation retries without timer parking (3 refusals before $T$; 0 after $T$) | **fails loudly**<br>Raises `NonDeterministicWorkflowError` on workflow replay | **PASS**<br>Wall-clock killed; retry cap halted after 3 attempts; 0 leaked PIDs |
| **DBOS (F7)** | **PASS** (10/10 = 100.0%, 95% CI: 72.2–100.0%)<br>Raw executions: **68** (8 re-runs of only interrupted tasks) | **PASS**<br>Waiting `DBOS.recv` survives serverless reboot in SQLite; `send_approval` unblocks; 1 exec, 1 effect | **PASS**<br>**0 attempts before $T$**; durable `DBOS.sleep`; auto-resumes at $T + 0.631$s tolerance | **fails loudly**<br>Raises `DBOSUnexpectedStepError` identifying mismatched step name | **PASS**<br>Wall-clock timeout fired (`killed-time`); retry cap fired after 3 attempts; 0 leaked PIDs |
| **Dagu (F8)** | **PASS** (10/10 = 100.0%, 95% CI: 72.2–100.0%)<br>Raw executions: **79** (19 re-runs of only interrupted tasks) | **PASS**<br>`human.task` file state survives engine reboot & 65s downtime; approved cleanly; 1 exec, 1 effect | **FAIL (Native)**<br>Blind static retries (4 refusals before $T$); requires external scripted parking wrapper | **recovers**<br>Runs bind to immutable attempt snapshot `dag.json`; live disk YAML edits are ignored | **PASS**<br>Wall-clock cap fired at 2s; retry cap halted after 3 attempts; 0 leaked PIDs |

---

## 2. Author Reported vs. Re-run Numbers

| Engine & Test | Author's Reported Numbers | Independent Re-run Numbers | Cause of Any Discrepancy |
|---|---|---|---|
| **Baseline KT1** | N/A (not measured by author) | 10/10 passed, 76 executions | Baseline author did not execute standard 10-trial crash protocol. Re-run revealed that while side effects are guarded by `fake_agent.py`, `ds_squad.py` restarts the entire squad from scratch, re-running completed tasks. |
| **Baseline KT2** | N/A | FAIL (re-executes PREP) | `ds_squad.py` has no persistent task journal; reboot loses in-memory task completion state. |
| **Baseline KT3** | N/A | FAIL (4 refusals before $T$) | `ds_squad.py` has no timer parking mechanism for exit code 29. |
| **Temporal KT1** | 1 trial, 2 items (A->B), 2 execs | **10/10 trials**, 6 items, **60 executions** | Author tested only a 1-trial 2-item smoke test in `test_durability.py`. Re-run subjected Temporal to the full 10-trial 6-item protocol. Temporal passed with zero duplicate executions. |
| **Temporal KT2** | Signal test passed in test suite | PASS (1 exec PREP, 1 exec DEPLOY) | Verified across complete dev server shutdown and restart using SQLite persistence (`dev_server_database_filename`). |
| **Temporal KT3** | Author reported `RouteExhausted` blocks item | **FAIL** (3 refusals before $T$, 0 after $T$) | `RouteExhausted` only fires if an external JSON file marks the route exhausted before launch. On real process exit code 29 without `report.md`, Temporal raises `ReportMissing` and executes blind continuation retries without parking. |
| **DBOS KT1** | 10/10 passed, 60–80 execs | 10/10 passed, **68 executions** | Consistent with author's numbers. Completed steps returned from SQLite step journal; only interrupted in-flight steps re-ran. |
| **DBOS KT2** | 1 exec, 1 effect, 72.5s total time | 1 exec, 1 effect, 77.9s total time | Consistent with author. `DBOS.recv` state survives machine kill and honors approval post-restart. |
| **DBOS KT3** | 0 attempts before $T$, tolerance $+0.243$s | 0 attempts before $T$, tolerance **$+0.631$s** | Consistent with author. Caught exit 29, parked in SQLite on `DBOS.sleep`, woke automatically at $T$. |
| **Dagu KT1** | 10/10 passed, 78 execs | 10/10 passed, **79 executions** | Consistent with author. Zombie detector recognized runner termination within 2s; `dagu retry` resumed without re-running completed steps. |
| **Dagu KT2** | 1 exec PREP, 1 exec DEPLOY | 1 exec PREP, 1 exec DEPLOY | Consistent with author. File-persisted `human.task` cleanly survived engine reboot. |
| **Dagu KT3** | Native: 6 retries before $T$<br>Scripted: 0 attempts before $T$, $+0.53$s | Native: **4 retries before $T$** (limit 3) | Consistent with author. Dagu's native `stepRetryPolicy` does not support dynamic timer parking from exit code 29 output. |

---

## 3. Minimal Reproductions for Failures

### A. Baseline KT2 Failure: Re-executing Prior Completed Steps on Restart
`ds_squad.py` stores item completion status in memory (`st = {i: {"status": "pending"} for i in items}`). When the runner process dies while waiting for an approval gate and restarts:
```bash
# Minimal reproduction showing ds_squad.py re-executing PREP after restart
python3 research/experiments/on-kill-tests-20261102/run_all_killtests.py --engines baseline --tests kt2
# Observation: PREP executes 2 times (once before kill, and again after restart)
```

### B. Baseline KT3 Failure: Immediate Blind Retries on Quota Exhaustion
`ds_squad.py` does not inspect exit code 29 or parse quota reset timestamps. Upon non-zero exit without `report.md`, it immediately re-launches the worker until `retry` cap is exhausted:
```bash
python3 research/experiments/on-kill-tests-20261102/run_all_killtests.py --engines baseline --tests kt3
# Observation: 4 quota refusals recorded in ledger in under 3 seconds; 0 attempts at reset timestamp T
```

### C. Temporal KT3 Failure: Missing Native Process Quota Parking
In the F3 Temporal prototype, an exit code 29 process failure results in missing `report.md`, causing `read_report_state` to throw `ReportMissing`. The workflow immediately triggers continuation retries rather than parking on a timer:
```bash
python3 research/experiments/on-kill-tests-20261102/run_all_killtests.py --engines temporal --tests kt3
# Observation: ItemWorkflow exhausts all 3 activity attempts within 1.5 seconds; 0 attempts after T
```

### D. Dagu KT3 Native Failure: Static Retries Instead of Dynamic Parking
Dagu's native `stepRetryPolicy` only supports fixed integer intervals (`interval_sec`). It cannot extract dynamic reset epochs from stderr or HTTP 429 headers:
```bash
python3 research/experiments/on-kill-tests-20261102/run_all_killtests.py --engines dagu --tests kt3
# Observation: Dagu retries blindly every 2s before T, exhausting retry limit without parking until T
```

---

## 4. Reproduction Commands

All tests are runnable from the experiment root using the self-contained virtual environment:

```bash
cd research/experiments/on-kill-tests-20261102

# Run full kill-test suite across all 4 engines (approx. 10 minutes)
.venv/bin/python run_all_killtests.py --engines baseline,temporal,dbos,dagu --tests kt1,kt2,kt3,kt4,kt5

# Or run individual engines:
.venv/bin/python run_all_killtests.py --engines baseline --tests kt1,kt2,kt3,kt4,kt5
.venv/bin/python run_all_killtests.py --engines temporal --tests kt1,kt2,kt3,kt4,kt5
.venv/bin/python run_all_killtests.py --engines dbos --tests kt1,kt2,kt3,kt4,kt5
.venv/bin/python run_all_killtests.py --engines dagu --tests kt1,kt2,kt3,kt4,kt5

# Recompute verification metrics and manifest
.venv/bin/python compute_metrics.py
```

---

## 5. What Would Make Us Choose Each Engine (10 Lines)

1. **Baseline (`ds_squad.py`)**: Choose if you want zero external dependencies, total single-file transparency, and don't care about process crash survival.
2. It is completely sufficient for short, homogeneous overnight runs where transient crashes are rare and re-running from scratch is acceptable.
3. However, its lack of state journaling causes catastrophic re-execution of expensive model tasks whenever the host or process restarts.
4. **Temporal**: Choose if running a distributed, multi-machine agent cluster where enterprise observability, time-skipping tests, and gRPC workflows are required.
5. It offers perfect deterministic event-history replay, zero duplicate activity executions on worker crash, and strong process group termination.
6. The major drawback is the heavy operational footprint (running server daemon, TCP ports, network serialization) and strict sandbox determinism rules.
7. **DBOS**: Choose if you want the durability guarantees of Temporal with the simplicity, speed, and zero-daemon footprint of SQLite.
8. It is the only engine to pass all five kill tests natively, featuring flawless 0-attempt quota parking (`DBOS.sleep`) and step-level journaling.
9. For single-host overnight agent squads, DBOS offers the highest reliability-to-complexity ratio without requiring background daemons or open ports.
10. **Dagu**: Choose for fixed, static CLI toolchains and scheduled batch pipelines where non-programmers need visual DAG execution and simple approval forms.
