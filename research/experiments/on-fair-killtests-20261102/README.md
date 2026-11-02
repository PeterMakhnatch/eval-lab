# F11-FAIR-KILLTESTS: Fair Kill-Test Comparison

Standardized, matched-protocol evaluation of 5 orchestration engines across 7 kill tests (KT1–KT7), incorporating the Phase 0 hardened baseline (`ds_squad_v2.py`) and real executions.

## Cohort
1. **Baseline Original (`ds_squad.py`)**: Legacy hand-rolled squad runner using in-memory state.
2. **Baseline Hardened (`ds_squad_v2.py`)**: Phase 0 hardened squad runner with `state.json` worker adoption (`AdoptedProc`), automatic HTTP 429 parking (`parked-quota`), and atomic `heartbeat.json`.
3. **Temporal (`temporalio>=1.35.0`)**: SQLite-backed dev server with Python workflows and activities.
4. **DBOS (`dbos>=3.4.0`)**: SQLite system database embedded step journaling engine.
5. **Dagu (`dagu v1.16+`)**: Go-based attempt directory snapshot orchestrator.

## Master Scorecard (KT1–KT7)

| Engine | KT1 Crash (Guarded) | KT1 Crash (Unguarded) | KT1 Max Overlap | KT2 Reboot Approval | KT3 Quota Park ($T=90\text{s}$) | KT4 Code Drift | KT5 Status Rows | KT6 Hang Latency | KT7 Worktree Race |
|---|---|---|---|---|---|---|---|---|---|
| **Baseline Original** | 0/10 (0.0%–27.8%) | 0/10 (0.0%–27.8%) | 0.0s | 0/3 (0.0%–56.1%) | FAIL (3 retries before $T$) | Silent divergence | PASS (in-memory) | 13.85s (runner cap) | Collides (4/12 fails) |
| **Baseline Hardened (R7)** | **10/10 (72.2%–100.0%)** | **10/10 (72.2%–100.0%)** | **0.0s** | **3/3 (43.9%–100.0%)** | **PASS (0 before $T$, $+1.517\text{s}$)** | Silent divergence | **PASS (`killed-time`, `failed`)** | 9.32s (runner cap) | **PASS (`fcntl.flock`, 12/12)** |
| **Temporal** | 9/10 (59.6%–98.2%) | 9/10 (59.6%–98.2%) | 0.0s | 2/3 (20.8%–93.9%) | **PASS (0 before $T$, $+0.034\text{s}$)** | Fails loudly (Replay mismatch stall) | **PASS (`failed`, timeout)** | **4.67s (`start_to_close`)** | Unprotected across runners |
| **DBOS** | 8/10 (49.0%–94.3%) | 9/10 (59.6%–98.2%) | 35.08s | 0/3 (0.0%–56.1%) | **PASS (0 before $T$, $+0.157\text{s}$)** | Fails loudly (`DBOSUnexpectedStepError`) | **PASS (`killed-time`, `failed`)** | 11.74s (step cap) | Unprotected across runners |
| **Dagu** | 10/10 (72.2%–100.0%) | 9/10 (59.6%–98.2%) | 36.78s | 0/3 (0.0%–56.1%) | **PASS (0 before $T$, $+0.235\text{s}$)** | Pinned snapshot (`dag.json`) | PASS (step timeout) | **4.42s (step timeout)** | Unprotected across runners |

## Reproduction Commands

From repository root:

```bash
# 1. Run unit test suite
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python -m pytest -q -p no:warnings research/experiments/on-fair-killtests-20261102/test_fair_killtests.py

# 2. Run KT3 Quota Parking test across all 5 engines (parallel)
uv run --no-sync python research/experiments/on-fair-killtests-20261102/test_kt3_parking.py

# 3. Run KT4 Code Change test across all 5 engines
uv run --no-sync python research/experiments/on-fair-killtests-20261102/test_kt4_code_change.py

# 4. Run KT5 Caps & Status Rows test across all 5 engines
uv run --no-sync python research/experiments/on-fair-killtests-20261102/test_kt5_caps.py

# 5. Run KT6 Child Hang test across all 5 engines
uv run --no-sync python research/experiments/on-fair-killtests-20261102/test_kt6_hang.py

# 6. Run KT7 Multi-runner Worktree Race test
uv run --no-sync python research/experiments/on-fair-killtests-20261102/test_kt7_worktree_race.py

# 7. Run full KT1 crash recovery suite (or specify --engine <name>)
uv run --no-sync python research/experiments/on-fair-killtests-20261102/test_kt1_suite.py --engine baseline_hardened

# 8. Recompute summary metrics and manifest
uv run --no-sync python research/experiments/on-fair-killtests-20261102/collect_receipts_and_compute.py
```
