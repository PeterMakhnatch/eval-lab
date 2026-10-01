# G2 data batch (HAR-120 x lf2): results

Generated 2026-10-01T10:38Z by `build_report.py` (research/experiments/har120-data-batch/build_report.py --live-root /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live --capture g2=/Users/petermakhnatch/Developer/eval-lab-results/har126-capture/g2 --capture g2b=/Users/petermakhnatch/Developer/eval-lab-results/har126-capture/g2b --capture g2c=/Users/petermakhnatch/Developer/eval-lab-results/har126-capture/g2c --sampler g2=/Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/g2-telemetry.jsonl --sampler g2b=/Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/g2b-telemetry.jsonl --sampler g2c=/Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/g2c-telemetry.jsonl --modal-billing research/experiments/har120-data-batch/modal-billing-20261001.json --out research/experiments/har120-data-batch).
Run worktree /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live at `8021fb8a`; harness lf2 `sha256:f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be`.
Scope: 30 tasks x 2 attempts = 60 cells; 64 jobs finished, 36 pending/absent.

Token rule: input/output tokens are PROXY-SETTLED ledger totals (`lab-metadata.json` provider_usage.totals), never tokens_proxy unverified and never native step sums (HAR-131/Cdx-2 defect; #609 merged and verified per trial below).
Stop rule: canonical stop prefers `diagnosis.exception_class`, then `token_flow.stop.stop_reason`, then the raw `stop_reason` field (the top-level field still reads `unknown` for harness loop-break stops; HAR-131 stop-vocabulary fix pending).

## 1. Wave-1 summary

Wave 1 (tick 1, 20 jobs, 08:01Z–09:02Z): 2 counted_pass, 15 counted_fail, 2 excluded (taint), 1 infra.
Settled tokens over wave-1 counted scope (pass + fail, 17 trials): 25,448,738 in / 222,220 out in 1,221 calls.
Raw verifier pass rate (counted scope): 2/17 = 11.8%.
r2 infra re-runs (22 of 40 specs finished, 09:23Z–09:51Z): 5 counted_pass, 14 counted_fail, 3 excluded (taint), 0 infra.
Settled tokens over r2 counted scope (pass + fail, 19 trials): 31,689,785 in / 231,482 out in 1,483 calls.
18 r2 specs not run (refused, lab defect): the tick refused them at 09:30:18–19Z with `dispatch_refused / quiet_failure_rule` because LoopBreakStop was missing from AGENT_STOP_EXCEPTIONS at ba8d8358 (fixed on main by #643). No job dirs exist; nothing is counted. Jobs: har120-001399-a2-r2, har120-001609-a2-r2, har120-001618-a2-r2, har120-001647-a2-r2, har120-001661-a2-r2, har120-001710-a2-r2, har120-001820-a2-r2, har120-001865-a2-r2, har120-001870-a2-r2, har120-001897-a2-r2, har120-002104-a2-r2, har120-002356-a2-r2, har120-002393-a2-r2, har120-002416-a2-r2, har120-002552-a2-r2, har120-002555-a2-r2, har120-002680-a2-r2, har120-002938-a2-r2.
G2 closes at 42 valid jobs: 20 wave-1 + 22 r2; the 18 refused r2 specs are not run and never counted.

## 2. Per-task table

| task | att | job | raw | counted | in / out (settled) | calls | stop | nudge -> stop | capture |
|---|---|---|---|---|---|---|---|---|---|---|
| 000227 | 1 | har120-000227-a1 | 0.0 | counted_fail | 2,451,194 / 4,838 | 99 | TrialBudgetExhausted (input-token ceiling) | nudge 19 (no stop) | trajectory_truncated 99/99 |
| 000227 | 2 | har120-000227-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000227 | 2 | har120-000227-a2-r2 | 0.0 | counted_fail | 2,435,135 / 4,513 | 96 | TrialBudgetExhausted (input-token ceiling) | nudge 10 (no stop) | trajectory_truncated 96/96 |
| 000341 | 1 | har120-000341-a1 | 1.0 | excluded:copied_fix+pass_tainted | 2,433,916 / 16,469 | 88 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 88/88 |
| 000341 | 2 | har120-000341-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000341 | 2 | har120-000341-a2-r2 | 0.0 | counted_fail | 2,447,593 / 12,802 | 96 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 96/96 |
| 000552 | 1 | har120-000552-a1 | 0.0 | counted_fail | 2,462,622 / 37,047 | 104 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 104/104 |
| 000552 | 2 | har120-000552-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000552 | 2 | har120-000552-a2-r2 | 1.0 | counted_pass | 947,067 / 11,032 | 54 | LoopBreakStop | 39 -> 54 | trajectory_truncated 54/54 |
| 000666 | 1 | har120-000666-a1 | 0.0 | counted_fail | 204,192 / 3,475 | 27 | confirmed completion | - | trajectory_truncated 27/27 |
| 000666 | 2 | har120-000666-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000666 | 2 | har120-000666-a2-r2 | 0.0 | counted_fail | 842,405 / 10,981 | 59 | LoopBreakStop | 44 -> 59 | trajectory_truncated 59/59 |
| 000803 | 1 | har120-000803-a1 | 0.0 | counted_fail | 580,852 / 6,332 | 42 | confirmed completion | - | trajectory_truncated 42/42 |
| 000803 | 2 | har120-000803-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000803 | 2 | har120-000803-a2-r2 | 0.0 | counted_fail | 2,463,424 / 11,961 | 107 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 107/107 |
| 000813 | 1 | har120-000813-a1 | 0.0 | counted_fail | 1,600,260 / 15,027 | 69 | LoopBreakStop | 54 -> 69 | trajectory_truncated 69/69 |
| 000813 | 2 | har120-000813-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000813 | 2 | har120-000813-a2-r2 | 0.0 | counted_fail | 2,463,536 / 29,369 | 103 | TrialBudgetExhausted (input-token ceiling) | nudge 26 (no stop) | trajectory_truncated 103/103 |
| 000838 | 1 | har120-000838-a1 | 1.0 | counted_pass | 1,904,526 / 11,269 | 83 | agent timeout | - | trajectory_truncated 83/83 |
| 000838 | 2 | har120-000838-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000838 | 2 | har120-000838-a2-r2 | 0.0 | counted_fail | 1,765,227 / 10,673 | 81 | LoopBreakStop | 66 -> 81 | trajectory_truncated 81/81 |
| 000865 | 1 | har120-000865-a1 | 0.0 | counted_fail | 71,629 / 1,434 | 33 | LoopBreakStop | 18 -> 33 | trajectory_truncated 33/33 |
| 000865 | 2 | har120-000865-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000865 | 2 | har120-000865-a2-r2 | 0.0 | counted_fail | 970,269 / 7,003 | 66 | LoopBreakStop | 51 -> 66 | trajectory_truncated 66/66 |
| 000941 | 1 | har120-000941-a1 | 0.0 | counted_fail | 2,460,129 / 10,547 | 102 | TrialBudgetExhausted (input-token ceiling) | nudge 45 (no stop) | trajectory_truncated 102/102 |
| 000941 | 2 | har120-000941-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 000941 | 2 | har120-000941-a2-r2 | 1.0 | counted_pass | 1,370,554 / 13,619 | 73 | LoopBreakStop | 58 -> 73 | trajectory_truncated 73/73 |
| 001265 | 1 | har120-001265-a1 | 0.0 | counted_fail | 615,767 / 9,605 | 60 | confirmed completion | - | trajectory_truncated 60/60 |
| 001265 | 2 | har120-001265-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001265 | 2 | har120-001265-a2-r2 | 0.0 | counted_fail | 2,435,348 / 8,846 | 98 | TrialBudgetExhausted (input-token ceiling) | nudge 63 (no stop) | trajectory_truncated 98/98 |
| 001269 | 1 | har120-001269-a1 | 0.0 | counted_fail | 2,426,017 / 23,482 | 92 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 92/92 |
| 001269 | 2 | har120-001269-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001269 | 2 | har120-001269-a2-r2 | 1.0 | excluded:copied_fix+pass_tainted | 2,446,342 / 17,714 | 101 | TrialBudgetExhausted (input-token ceiling) | nudge 37 (no stop) | trajectory_truncated 101/101 |
| 001373 | 1 | har120-001373-a1 | 0.0 | counted_fail | 1,196,234 / 8,297 | 71 | confirmed completion | - | trajectory_truncated 71/71 |
| 001373 | 2 | har120-001373-a2 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001373 | 2 | har120-001373-a2-r2 | 1.0 | excluded:copied_fix+pass_tainted | 233,429 / 5,013 | 30 | confirmed completion | - | trajectory_truncated 30/30 |
| 001399 | 1 | har120-001399-a1 | 0.0 | counted_fail | 1,758,425 / 17,294 | 87 | LoopBreakStop | 71 -> 86 | trajectory_truncated 87/87 |
| 001399 | 2 | har120-001399-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001399 | 2 | har120-001399-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001609 | 1 | har120-001609-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001609 | 1 | har120-001609-a1-r2 | 0.0 | counted_fail | 1,954,851 / 20,091 | 75 | LoopBreakStop | 60 -> 75 | trajectory_truncated 75/75 |
| 001609 | 2 | har120-001609-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001609 | 2 | har120-001609-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001618 | 1 | har120-001618-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001618 | 1 | har120-001618-a1-r2 | 0.0 | counted_fail | 1,201,329 / 12,633 | 79 | LoopBreakStop | 64 -> 79 | trajectory_truncated 79/79 |
| 001618 | 2 | har120-001618-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001618 | 2 | har120-001618-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001647 | 1 | har120-001647-a1 | None | infra | - / - | 0 | infra (tmux missing) | - | capture_missing 0/0 |
| 001647 | 2 | har120-001647-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001647 | 2 | har120-001647-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001661 | 1 | har120-001661-a1 | 0.0 | counted_fail | 1,189,308 / 17,722 | 56 | LoopBreakStop | 41 -> 56 | trajectory_truncated 56/56 |
| 001661 | 2 | har120-001661-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001661 | 2 | har120-001661-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001710 | 1 | har120-001710-a1 | 0.0 | counted_fail | 2,082,993 / 5,648 | 94 | LoopBreakStop | 79 -> 94 | trajectory_truncated 94/94 |
| 001710 | 2 | har120-001710-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001710 | 2 | har120-001710-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001820 | 1 | har120-001820-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001820 | 1 | har120-001820-a1-r2 | 0.0 | counted_fail | 315,215 / 5,016 | 32 | confirmed completion | - | trajectory_truncated 32/32 |
| 001820 | 2 | har120-001820-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001820 | 2 | har120-001820-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001865 | 1 | har120-001865-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001865 | 1 | har120-001865-a1-r2 | 0.0 | counted_fail | 1,168,348 / 10,980 | 59 | LoopBreakStop | 44 -> 59 | trajectory_truncated 59/59 |
| 001865 | 2 | har120-001865-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001865 | 2 | har120-001865-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001870 | 1 | har120-001870-a1 | 1.0 | excluded:copied_fix+pass_tainted | 2,365,815 / 32,329 | 120 | TrialBudgetExhausted (request ceiling) | - | trajectory_truncated 120/120 |
| 001870 | 2 | har120-001870-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001870 | 2 | har120-001870-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 001897 | 1 | har120-001897-a1 | 0.0 | counted_fail | 1,501,027 / 27,897 | 62 | LoopBreakStop | 45 -> 60 | trajectory_truncated 62/62 |
| 001897 | 2 | har120-001897-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001897 | 2 | har120-001897-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002104 | 1 | har120-002104-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002104 | 1 | har120-002104-a1-r2 | 0.0 | counted_fail | 2,413,962 / 17,295 | 85 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 85/85 |
| 002104 | 2 | har120-002104-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002104 | 2 | har120-002104-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002356 | 1 | har120-002356-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002356 | 1 | har120-002356-a1-r2 | 1.0 | excluded:copied_fix+pass_tainted | 1,592,090 / 11,918 | 73 | LoopBreakStop | 58 -> 73 | trajectory_truncated 73/73 |
| 002356 | 2 | har120-002356-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002356 | 2 | har120-002356-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002393 | 1 | har120-002393-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002393 | 1 | har120-002393-a1-r2 | 0.0 | counted_fail | 275,421 / 3,705 | 40 | LoopBreakStop | 25 -> 40 | trajectory_truncated 40/40 |
| 002393 | 2 | har120-002393-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002393 | 2 | har120-002393-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002416 | 1 | har120-002416-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002416 | 1 | har120-002416-a1-r2 | 1.0 | counted_pass | 2,265,118 / 13,468 | 120 | TrialBudgetExhausted (request ceiling) | - | trajectory_truncated 120/120 |
| 002416 | 2 | har120-002416-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002416 | 2 | har120-002416-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002552 | 1 | har120-002552-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002552 | 1 | har120-002552-a1-r2 | 1.0 | counted_pass | 2,464,413 / 11,815 | 95 | TrialBudgetExhausted (input-token ceiling) | nudge 59 (no stop) | trajectory_truncated 95/95 |
| 002552 | 2 | har120-002552-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002552 | 2 | har120-002552-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002555 | 1 | har120-002555-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002555 | 1 | har120-002555-a1-r2 | 1.0 | counted_pass | 1,490,570 / 15,680 | 65 | LoopBreakStop | 50 -> 65 | trajectory_truncated 65/65 |
| 002555 | 2 | har120-002555-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002555 | 2 | har120-002555-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002680 | 1 | har120-002680-a1 | 0.0 | counted_fail | 2,458,142 / 17,377 | 89 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 89/89 |
| 002680 | 2 | har120-002680-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002680 | 2 | har120-002680-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |
| 002938 | 1 | har120-002938-a1 | 1.0 | counted_pass | 485,421 / 4,929 | 51 | LoopBreakStop | 36 -> 51 | trajectory_truncated 51/51 |
| 002938 | 2 | har120-002938-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002938 | 2 | har120-002938-a2-r2 | None | not_run: quiet_failure_rule refusal (LoopBreakStop missing from AGENT_STOP_EXCEPTIONS at ba8d8358; fixed in 8021fb8a) | - / - | - | refused | - | n/a (refused; lab defect, see section 1) |

## 3. Infra-failed originals (no counted outcome)

- har120-000227-a2 (000227 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000227-a2-r2` (landed).
- har120-000341-a2 (000341 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000341-a2-r2` (landed).
- har120-000552-a2 (000552 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000552-a2-r2` (landed).
- har120-000666-a2 (000666 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000666-a2-r2` (landed).
- har120-000803-a2 (000803 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000803-a2-r2` (landed).
- har120-000813-a2 (000813 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000813-a2-r2` (landed).
- har120-000838-a2 (000838 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000838-a2-r2` (landed).
- har120-000865-a2 (000865 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000865-a2-r2` (landed).
- har120-000941-a2 (000941 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-000941-a2-r2` (landed).
- har120-001265-a2 (001265 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-001265-a2-r2` (landed).
- har120-001269-a2 (001269 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-001269-a2-r2` (landed).
- har120-001373-a2 (001373 attempt 2): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-001373-a2-r2` (landed).
- har120-001609-a1 (001609 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-001609-a1-r2` (landed).
- har120-001618-a1 (001618 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-001618-a1-r2` (landed).
- har120-001647-a1 (001647 attempt 1): infra (tmux missing), raw_reward=null, reasons=infra; superseded by `har120-001647-a1-r2` (no r2 spec).
- har120-001820-a1 (001820 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-001820-a1-r2` (landed).
- har120-001865-a1 (001865 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-001865-a1-r2` (landed).
- har120-002104-a1 (002104 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-002104-a1-r2` (landed).
- har120-002356-a1 (002356 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-002356-a1-r2` (landed).
- har120-002393-a1 (002393 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-002393-a1-r2` (landed).
- har120-002416-a1 (002416 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-002416-a1-r2` (landed).
- har120-002552-a1 (002552 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-002552-a1-r2` (landed).
- har120-002555-a1 (002555 attempt 1): infra (upstream 503), raw_reward=null, reasons=infra; superseded by `har120-002555-a1-r2` (landed).

## 4. counted_pass trials

7 counted_pass trials (see `counted_pass.jsonl` for Traces/Data):
- 000552 attempt 2: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000552-a2-r2 trial `har120-000552-a2-r2__ptsTP5m` reward=1.0
- 000838 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000838-a1 trial `har120-000838-a1__YSe3G7t` reward=1.0
- 000941 attempt 2: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000941-a2-r2 trial `har120-000941-a2-r2__VrxFqqG` reward=1.0
- 002416 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002416-a1-r2 trial `har120-002416-a1-r2__ccd9JcE` reward=1.0
- 002552 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002552-a1-r2 trial `har120-002552-a1-r2__ZftRzg3` reward=1.0
- 002555 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002555-a1-r2 trial `har120-002555-a1-r2__Uki9j6d` reward=1.0
- 002938 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002938-a1 trial `har120-002938-a1__UpSjiuM` reward=1.0

## 5. GEPA seed receipt (HAR-135, 04:34Z freeze)

lf2 harness: `research/experiments/har126-lf2/harness-lf2` digest `sha256:f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be`.
Representative stock spec: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/research/experiments/har120-data-batch/specs/har120-000803-a1.json` sha256:8d8fc8fd9a944b6e1253b74ab7c5c2359bafddf131038d943f73080aa9e23426 limits: max_requests=120 max_input_tokens=2500000 max_output_tokens=131072 model=selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B env=daytona timeout_s=3600 concurrency=1.
Per-task attempt-1/attempt-2 job paths (result, config, lock, metadata, processed counts):
- 001647 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001647-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001647-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001647-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001647-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001647-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001647-a1/processed/trial-har120-001647-a1__iqMRm5R.json [outcome=infra]
  attempt 2: PENDING (absent)
- 000803 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a1/processed/trial-har120-000803-a1__xWzxKCn.json [outcome=counted_fail]
  attempt 2: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a2-r2/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a2-r2/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a2-r2/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a2-r2/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a2-r2/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000803-a2-r2/processed/trial-har120-000803-a2-r2__QyoYej6.json [outcome=counted_fail]
- 001870 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001870-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001870-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001870-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001870-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001870-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001870-a1/processed/trial-har120-001870-a1__rXFPYLR.json [outcome=excluded:copied_fix+pass_tainted]
  attempt 2: PENDING (absent)
- 000341 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a1/processed/trial-har120-000341-a1__MYkGYQZ.json [outcome=excluded:copied_fix+pass_tainted]
  attempt 2: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a2-r2/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a2-r2/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a2-r2/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a2-r2/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a2-r2/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-000341-a2-r2/processed/trial-har120-000341-a2-r2__MSDHRHS.json [outcome=counted_fail]
- 001897 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001897-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001897-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001897-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001897-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001897-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001897-a1/processed/trial-har120-001897-a1__6AyLy9W.json [outcome=counted_fail]
  attempt 2: PENDING (absent)
- 002938 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002938-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002938-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002938-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002938-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002938-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002938-a1/processed/trial-har120-002938-a1__UpSjiuM.json [outcome=counted_pass]
  attempt 2: PENDING (absent)
- 001710 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001710-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001710-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001710-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001710-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001710-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001710-a1/processed/trial-har120-001710-a1__BLMKCZN.json [outcome=counted_fail]
  attempt 2: PENDING (absent)
- 001399 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001399-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001399-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001399-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001399-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001399-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001399-a1/processed/trial-har120-001399-a1__fwLaw2U.json [outcome=counted_fail]
  attempt 2: PENDING (absent)
- 002680 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002680-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002680-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002680-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002680-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002680-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-002680-a1/processed/trial-har120-002680-a1__Ci4bEkf.json [outcome=counted_fail]
  attempt 2: PENDING (absent)
- 001661 attempt 1: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001661-a1/result.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001661-a1/config.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001661-a1/lock.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001661-a1/lab-metadata.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001661-a1/processed/job.json /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live/runs/har120-001661-a1/processed/trial-har120-001661-a1__xdRDCYr.json [outcome=counted_fail]
  attempt 2: PENDING (absent)

## 6. Telemetry

Round g2: 42 trials, 1418 calls, latency p50=5.282337s p90=24.354619s, peak call concurrency=19, mean=4.34; trajectory-vs-ledger matched 13/42 (503-infra trials mismatch trivially: no trajectory; full list in telemetry-extract-g2.json).
Round g2c: 22 trials, 1684 calls, latency p50=5.215785s p90=13.285194s, peak call concurrency=20, mean=9.24; trajectory-vs-ledger matched 21/22 (503-infra trials mismatch trivially: no trajectory; full list in telemetry-extract-g2c.json).
Sampler g2: 233 probes (233 healthy): running peak=13.0 mean=1.19; queue peak=0.0 mean=0.0; gen peak=694.4 mean=72.0 tok/s; token usage peak=0.35 mean=0.035.
Sampler g2b: 11 probes, 11 SGLang-503 (cold upstream); no healthy gauges.
Sampler g2c: 107 probes (107 healthy): running peak=17.0 mean=2.46; queue peak=0.0 mean=0.0; gen peak=659.7 mean=156.7 tok/s; token usage peak=0.31 mean=0.072.
Capture g2: 4319 calls, upstream latency p50=1.59s p90=4.9s, errors=0, status={'200': 3725, '503': 594}.
Capture g2b: 0 calls (capture file empty).
Capture g2c: 0 calls (capture file empty).

### Infra note (HAR-129)

- Tick-boundary teardown gap: tick 1's teardown stopped the Modal app at 09:02Z even though 40 re-estimated specs were approved; the redeploy's warm smoke returned HTTP 503 while cold-starting, the round script did not abort, and 22 dispatched specs ended ServiceUnavailableError with 0 tokens (plus 18 quiet-failure refusals). Cleared by the Q1 $0 local-Docker nop; r2 redeployed with a must-pass warm check.
- Wave (non-refill) dispatch: within tick 1, queue `waiting` held 39 refused specs while `running` drained 20 -> 2, so no mid-tick refill was observable; whether a tick refills freed slots when dispatchable specs exist is untested [INFERENCE]. SGLang served ~2 running requests at 153-250 tok/s at the tail vs ~400 tok/s under full 20-way load.

## 7. Capture-link status

- har120-000227-a1: assigned=99 unassigned=4220 ambiguous=0 verdict=trajectory_truncated. lines 3-931 bytes 12462-55224980.
- har120-000227-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1451-1892 bytes 112654677-115234740.
- har120-000227-a2-r2: assigned=96 unassigned=4223 ambiguous=0 verdict=trajectory_truncated. lines 2029-2992 bytes 116072417-174298005.
- har120-000341-a1: assigned=88 unassigned=4231 ambiguous=0 verdict=trajectory_truncated. lines 6-1079 bytes 33466-72676609.
- har120-000341-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1443-1880 bytes 112614469-115159339.
- har120-000341-a2-r2: assigned=96 unassigned=4223 ambiguous=0 verdict=trajectory_truncated. lines 2027-3190 bytes 116059836-194149159.
- har120-000552-a1: assigned=104 unassigned=4215 ambiguous=0 verdict=trajectory_truncated. lines 7-1350 bytes 40221-104570207.
- har120-000552-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1463-1910 bytes 112720050-115353104.
- har120-000552-a2-r2: assigned=54 unassigned=4265 ambiguous=0 verdict=trajectory_truncated. lines 2037-2831 bytes 116133761-158410525.
- har120-000666-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=trajectory_truncated. lines 70-362 bytes 755744-9745753.
- har120-000666-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1432-1862 bytes 112558046-115059643.
- har120-000666-a2-r2: assigned=59 unassigned=4260 ambiguous=0 verdict=trajectory_truncated. lines 2026-3294 bytes 116053708-204875296.
- har120-000803-a1: assigned=42 unassigned=4277 ambiguous=0 verdict=trajectory_truncated. lines 67-619 bytes 736788-26050803.
- har120-000803-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1434-1873 bytes 112568712-115119149.
- har120-000803-a2-r2: assigned=107 unassigned=4212 ambiguous=0 verdict=trajectory_truncated. lines 2108-3316 bytes 117008533-207160671.
- har120-000813-a1: assigned=69 unassigned=4250 ambiguous=0 verdict=trajectory_truncated. lines 68-994 bytes 742189-61801662.
- har120-000813-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1430-1849 bytes 112546330-114987887.
- har120-000813-a2-r2: assigned=103 unassigned=4216 ambiguous=0 verdict=trajectory_truncated. lines 2033-3462 bytes 116104017-219647196.
- har120-000838-a1: assigned=83 unassigned=4236 ambiguous=0 verdict=trajectory_truncated. lines 5-1429 bytes 27743-112546330.
- har120-000838-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1452-1895 bytes 112661496-115253307.
- har120-000838-a2-r2: assigned=81 unassigned=4238 ambiguous=0 verdict=trajectory_truncated. lines 2034-3704 bytes 116111689-244365596.
- har120-000865-a1: assigned=33 unassigned=4286 ambiguous=0 verdict=trajectory_truncated. lines 8-876 bytes 48566-49138931.
- har120-000865-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1438-1881 bytes 112589133-115163742.
- har120-000865-a2-r2: assigned=66 unassigned=4253 ambiguous=0 verdict=trajectory_truncated. lines 2028-2947 bytes 116066624-169270136.
- har120-000941-a1: assigned=102 unassigned=4217 ambiguous=0 verdict=trajectory_truncated. lines 69-1046 bytes 749839-67954978.
- har120-000941-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1440-1882 bytes 112597721-115168423.
- har120-000941-a2-r2: assigned=73 unassigned=4246 ambiguous=0 verdict=trajectory_truncated. lines 2110-3472 bytes 117030225-220569498.
- har120-001265-a1: assigned=60 unassigned=4259 ambiguous=0 verdict=trajectory_truncated. lines 25-1103 bytes 203050-75561360.
- har120-001265-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1514-1935 bytes 113026540-115512617.
- har120-001265-a2-r2: assigned=98 unassigned=4221 ambiguous=0 verdict=trajectory_truncated. lines 2063-3524 bytes 116383770-225467906.
- har120-001269-a1: assigned=92 unassigned=4227 ambiguous=0 verdict=trajectory_truncated. lines 33-1351 bytes 288121-104757908.
- har120-001269-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1973-2023 bytes 115748798-116039539.
- har120-001269-a2-r2: assigned=101 unassigned=4218 ambiguous=0 verdict=trajectory_truncated. lines 2835-3673 bytes 158703999-240833947.
- har120-001373-a1: assigned=71 unassigned=4248 ambiguous=0 verdict=trajectory_truncated. lines 24-1403 bytes 194441-109370878.
- har120-001373-a2: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1970-2020 bytes 115726779-116026807.
- har120-001373-a2-r2: assigned=30 unassigned=4289 ambiguous=0 verdict=trajectory_truncated. lines 2905-3554 bytes 165267586-228051285.
- har120-001399-a1: assigned=87 unassigned=4232 ambiguous=0 verdict=trajectory_truncated. lines 1-1266 bytes 0-96624550.
- har120-001609-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1654-1962 bytes 113834093-115680013.
- har120-001609-a1-r2: assigned=75 unassigned=4244 ambiguous=0 verdict=trajectory_truncated. lines 2030-3166 bytes 116080622-191909248.
- har120-001618-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1481-1918 bytes 112826901-115404800.
- har120-001618-a1-r2: assigned=79 unassigned=4240 ambiguous=0 verdict=trajectory_truncated. lines 2058-3623 bytes 116334120-234744711.
- har120-001647-a1: assigned=0 unassigned=4319 ambiguous=0 verdict=capture_missing. bounds=n/a.
- har120-001661-a1: assigned=56 unassigned=4263 ambiguous=0 verdict=trajectory_truncated. lines 59-1251 bytes 635724-94615786.
- har120-001710-a1: assigned=94 unassigned=4225 ambiguous=0 verdict=trajectory_truncated. lines 2-922 bytes 6199-54251798.
- har120-001820-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1466-1911 bytes 112740573-115360726.
- har120-001820-a1-r2: assigned=32 unassigned=4287 ambiguous=0 verdict=trajectory_truncated. lines 2032-3080 bytes 116095124-183040010.
- har120-001865-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1740-1969 bytes 114319756-115726779.
- har120-001865-a1-r2: assigned=59 unassigned=4260 ambiguous=0 verdict=trajectory_truncated. lines 2031-2833 bytes 116087568-158588270.
- har120-001870-a1: assigned=120 unassigned=4199 ambiguous=0 verdict=trajectory_truncated. lines 27-1317 bytes 222437-100515197.
- har120-001897-a1: assigned=62 unassigned=4257 ambiguous=0 verdict=trajectory_truncated. lines 60-1178 bytes 642976-85711895.
- har120-002104-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1436-1872 bytes 112577705-115114965.
- har120-002104-a1-r2: assigned=85 unassigned=4234 ambiguous=0 verdict=trajectory_truncated. lines 2025-3291 bytes 116046276-204508579.
- har120-002356-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1437-1871 bytes 112583857-115108813.
- har120-002356-a1-r2: assigned=73 unassigned=4246 ambiguous=0 verdict=trajectory_truncated. lines 2024-3502 bytes 116039539-223445148.
- har120-002393-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1541-1940 bytes 113178694-115538590.
- har120-002393-a1-r2: assigned=40 unassigned=4279 ambiguous=0 verdict=trajectory_truncated. lines 2069-2724 bytes 116449921-148530344.
- har120-002416-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1714-1967 bytes 114173910-115714661.
- har120-002416-a1-r2: assigned=120 unassigned=4199 ambiguous=0 verdict=trajectory_truncated. lines 2036-3417 bytes 116124979-215749923.
- har120-002552-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1503-1926 bytes 112949252-115460190.
- har120-002552-a1-r2: assigned=95 unassigned=4224 ambiguous=0 verdict=trajectory_truncated. lines 2066-3590 bytes 116414754-231671216.
- har120-002555-a1: assigned=27 unassigned=4292 ambiguous=0 verdict=complete. lines 1441-1878 bytes 112602402-115149541.
- har120-002555-a1-r2: assigned=65 unassigned=4254 ambiguous=0 verdict=trajectory_truncated. lines 2035-3710 bytes 116117409-245164250.
- har120-002680-a1: assigned=89 unassigned=4230 ambiguous=0 verdict=trajectory_truncated. lines 99-1325 bytes 1170295-101667098.
- har120-002938-a1: assigned=51 unassigned=4268 ambiguous=0 verdict=trajectory_truncated. lines 53-538 bytes 559661-20093112.

Linked 64 finished jobs, 0 without receipt.
Frozen capture file `/Users/petermakhnatch/Developer/eval-lab-results/har126-capture/g2/calls.jsonl` sha256:ada914c85c9d0132af618b387ae97425db644e11f4e8bbac1f9163841ea20597 292044252 bytes, 4319 lines, 70 route tokens. Only the g2 server ever bound its port; g2b/g2c/c1 dirs are empty.
- wave 1: lines 1-1429, bytes 0-112546330, 1429 calls.
- g2b failures: lines 1430-2023, bytes 112546330-116039539, 594 calls.
- r2: lines 2024-3710, bytes 116039539-245164250, 1687 calls.
Non-G2 (C1, HAR-135) ranges in the same file — exclude for G3:
- har135-c1-000803: lines 3717-4064, bytes 245216402-267225619, 38 calls.
- har135-c1-001399: lines 3713-4039, bytes 245180589-264499419, 59 calls.
- har135-c1-001661: lines 3712-4156, bytes 245172073-278788225, 59 calls.
- har135-c1-001710: lines 3716-4253, bytes 245208363-286908324, 105 calls.
- har135-c1-001897: lines 3715-4276, bytes 245198626-289630553, 119 calls.
- har135-c1-002680: lines 3738-4319, bytes 245486005-292044252, 109 calls.
- har135-c1-002938: lines 3711-4082, bytes 245164250-269423526, 120 calls.

## 8. Maintenance performed by this run

- telemetry extract g2: calls=1418 p50=5.28s p90=24.35s peak_call_concurrency=19 peak_trial_concurrency=20
- telemetry extract g2c: calls=1684 p50=5.22s p90=13.29s peak_call_concurrency=20 peak_trial_concurrency=20

## 9. Spend split

- Daytona wave1: $1.0553 over 20 trials (trial lifetimes x $0.23094/h).
- Daytona g2b_failures: $0.0833 over 22 trials (trial lifetimes x $0.23094/h).
- Daytona r2: $1.0286 over 22 trials (trial lifetimes x $0.23094/h).
- Daytona C1: $0.2749 over 7 trials (trial lifetimes x $0.23094/h; HAR-135 scope, not G2).
- Modal evallab-mimo-v26-9b hourly: {'2026-10-01T00': 1.5338, '2026-10-01T01': 0.6433, '2026-10-01T02': 1.0627, '2026-10-01T04': 0.2629, '2026-10-01T07': 0.7622, '2026-10-01T08': 2.8172, '2026-10-01T09': 2.3493, '2026-10-01T10': 0.7219}.
- Modal windows: {'har116_00h00_02h30': 3.2398, 'smoke_07h27_07h40': 0.7622, 'g2_07h57_09h51': 5.9287, 'c1_09h51_10h15': 0.7219}.
- Modal non-G2 apps (excluded): $0.6513.
  Caveat: hour buckets do not align to windows: the 07h bucket holds the 07:27 smoke and G2's 07:57 launch; the 09h bucket holds G2's redeploys, the r2 tail and C1's 09:51 start.
  Caveat: the 10h bucket is partial (covers C1's 10:00-10:15 tail and anything after).
  Caveat: other_apps_usd (lora/sft/smoke apps) is Infra G4 scope, excluded from G2/C1/smoke sums.

