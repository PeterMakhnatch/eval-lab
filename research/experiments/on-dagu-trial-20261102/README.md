> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.

# Dagu Trial for Fixed CLI-Agent Batches (F8-DAGU-TRIAL)

This directory contains the artifacts, test harnesses, DAG specifications, and results for the trial of **Dagu v2.24.0** (GPL-3.0 single Go binary) evaluating durability, human approval persistence, quota parking, code mutations under live runs, and execution caps across the five standardized overnight kill tests.

## Artifact Inventory

- `squad_plan.yaml`: Canonical squad plan in Dagu YAML featuring two dependency chains of 3 fake-agent steps (`A1->A2->A3`, `B1->B2->B3`), `max_active_steps: 2`, retry policies, and a final attested real `omp -p` step invoking `google-antigravity/gemini-3.8-flash`.
- `kt1_squad.yaml`: Engine-neutral 6-item squad DAG for KT1 crash trials.
- `kt2_approval.yaml`: Human-in-the-loop DAG featuring a processless `human.task` approval gate with input schema.
- `fake_agent.py`: Shared engine-neutral fixture implementing execution tracking, idempotent side-effects, and quota refusals (`--id`, `--ledger`, `--report`, `--seconds`, `--quota-until`).
- `failing_step.py`: Helper script for deterministic non-zero exit code testing under iteration caps.
- `run_kt1.py`: Harness for KT1 (10 crash trials with seeded random SIGKILL delays).
- `run_kt2.py`: Harness for KT2 (human approval persistence across engine restart with 65s host downtime and 65s post-restart wait).
- `run_kt3.py`: Harness for KT3 (comparative evaluation of native Dagu retry policy vs scripted quota parking adapter).
- `run_kt4.py`: Harness for KT4 (live DAG code mutations: adding steps and renaming steps while in-flight).
- `run_kt5.py`: Harness for KT5 (wall-clock timeout cap and retry loop iteration cap).
- `compute_manifest.py`: Cryptographic manifest generator computing sha256 checksums across all receipts and scripts.
- `manifest.json`: Full SHA256 checksum manifest.

## Summary of the Five Kill Tests

| Test | Objective | Target / Condition | Observed Outcome | Verdict |
|---|---|---|---|---|
| **KT1 Crash** | SIGKILL runner during execution; restart; 10 trials | $\ge 9/10$ trials complete with exact-1 effect per item | 10/10 trials completed; exact-1 effect on all 6 items; 0 human steps | **PASS** (10/10) |
| **KT2 Approval** | Persist `human.task` across engine kill and >60s downtime | Honor approval $\ge 60$s after restart; no state lost; nothing re-asked | Run remained in `Waiting` state across reboot; approved at T+138s; `PREP` not re-executed; `DEPLOY` executed | **PASS** |
| **KT3 Quota Parking** | Park until $T = \text{now} + 30$s on code 29 with zero attempts before $T$ | Zero execution attempts before $T$; automatic resume at $T$ | Native: **FAIL** (6 retries before $T$). Scripted: **PASS** (0 attempts before $T$, resumed at $T + 0.53$s) | **FAIL (Native)** / **PASS (Scripted)** |
| **KT4 Live Edit** | Add step & rename step in YAML while live run is in-flight | Classify: recovers, fails loudly, or silently diverges | **RECOVERS** (Pinned snapshot: Dagu archives `dag.json` per attempt; live edits to disk YAML are ignored by in-flight retries) | **RECOVERS** |
| **KT5 Caps** | Wall-clock timeout & retry iteration cap stop run & leave rows | Both caps fire, both leave status rows, 0 leaked child processes | Wall-clock cap fired at 3.17s (limit 3s); retry cap fired after 4 attempts (limit 3); 0 leaked processes | **PASS** |

## Binary Provenance

- **Binary**: Dagu v2.24.0 (`darwin_arm64`)
- **License**: GNU General Public License Version 3 (GPL-3.0)
- **SHA256**: `8c9b7d12caf218beb5203ea5b82614944277c5f37c54ca03f156406d8d5f6728`
- **Location**: `/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F8/dagu/dagu` (untracked, local)

## Reproduction Commands

```bash
# 1. Validate squad plan and smoke test with short duration
dagu validate research/experiments/on-dagu-trial-20261102/squad_plan.yaml
dagu start squad_plan -- ITEM_SECONDS=1

# 2. KT1 Crash (10 trials)
python3 research/experiments/on-dagu-trial-20261102/run_kt1.py --trials 10 --seconds 20 --seed 42

# 3. KT2 Human Approval Across Restart
python3 research/experiments/on-dagu-trial-20261102/run_kt2.py

# 4. KT3 Quota Parking Investigation
python3 research/experiments/on-dagu-trial-20261102/run_kt3.py

# 5. KT4 Code Mutation Under Live Run
python3 research/experiments/on-dagu-trial-20261102/run_kt4.py

# 6. KT5 Caps (Wall-Clock and Iteration)
python3 research/experiments/on-dagu-trial-20261102/run_kt5.py
```
