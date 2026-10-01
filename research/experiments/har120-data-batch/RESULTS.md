# G2 data batch (HAR-120 x lf2): results

Generated 2026-10-01T10:11Z by `build_report.py` (research/experiments/har120-data-batch/build_report.py --live-root /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live --capture g2=/Users/petermakhnatch/Developer/eval-lab-results/har126-capture/g2 --capture g2b=/Users/petermakhnatch/Developer/eval-lab-results/har126-capture/g2b --capture g2c=/Users/petermakhnatch/Developer/eval-lab-results/har126-capture/g2c --sampler g2=/Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/g2-telemetry.jsonl --sampler g2b=/Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/g2b-telemetry.jsonl --sampler g2c=/Users/petermakhnatch/Developer/eval-lab-results/2026-10-01/g2c-telemetry.jsonl --out research/experiments/har120-data-batch --no-maintenance).
Run worktree /Users/petermakhnatch/Developer/eval-lab/.worktrees/har126-live at `ba8d8358`; harness lf2 `sha256:f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be`.
Scope: 30 tasks x 2 attempts = 60 cells; 64 jobs finished, 36 pending/absent.

Token rule: input/output tokens are PROXY-SETTLED ledger totals (`lab-metadata.json` provider_usage.totals), never tokens_proxy unverified and never native step sums (HAR-131/Cdx-2 defect; #609 merged and verified per trial below).
Stop rule: canonical stop prefers `diagnosis.exception_class`, then `token_flow.stop.stop_reason`, then the raw `stop_reason` field (the top-level field still reads `unknown` for harness loop-break stops; HAR-131 stop-vocabulary fix pending).

## 1. Wave-1 summary

Wave 1 (tick 1, 20 jobs, 08:01Z–09:02Z): 2 counted_pass, 15 counted_fail, 2 excluded (taint), 1 infra.
Settled tokens over wave-1 counted scope (pass + fail, 17 trials): 25,448,738 in / 222,220 out in 1,221 calls.
Raw verifier pass rate (counted scope): 2/17 = 11.8%.

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
| 001609 | 1 | har120-001609-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001609 | 1 | har120-001609-a1-r2 | 0.0 | counted_fail | 1,954,851 / 20,091 | 75 | LoopBreakStop | 60 -> 75 | trajectory_truncated 75/75 |
| 001609 | 2 | har120-001609-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001618 | 1 | har120-001618-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001618 | 1 | har120-001618-a1-r2 | 0.0 | counted_fail | 1,201,329 / 12,633 | 79 | LoopBreakStop | 64 -> 79 | trajectory_truncated 79/79 |
| 001618 | 2 | har120-001618-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001647 | 1 | har120-001647-a1 | None | infra | - / - | 0 | infra (tmux missing) | - | capture_missing 0/0 |
| 001647 | 2 | har120-001647-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001661 | 1 | har120-001661-a1 | 0.0 | counted_fail | 1,189,308 / 17,722 | 56 | LoopBreakStop | 41 -> 56 | trajectory_truncated 56/56 |
| 001661 | 2 | har120-001661-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001710 | 1 | har120-001710-a1 | 0.0 | counted_fail | 2,082,993 / 5,648 | 94 | LoopBreakStop | 79 -> 94 | trajectory_truncated 94/94 |
| 001710 | 2 | har120-001710-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001820 | 1 | har120-001820-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001820 | 1 | har120-001820-a1-r2 | 0.0 | counted_fail | 315,215 / 5,016 | 32 | confirmed completion | - | trajectory_truncated 32/32 |
| 001820 | 2 | har120-001820-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001865 | 1 | har120-001865-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 001865 | 1 | har120-001865-a1-r2 | 0.0 | counted_fail | 1,168,348 / 10,980 | 59 | LoopBreakStop | 44 -> 59 | trajectory_truncated 59/59 |
| 001865 | 2 | har120-001865-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001870 | 1 | har120-001870-a1 | 1.0 | excluded:copied_fix+pass_tainted | 2,365,815 / 32,329 | 120 | TrialBudgetExhausted (request ceiling) | - | trajectory_truncated 120/120 |
| 001870 | 2 | har120-001870-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 001897 | 1 | har120-001897-a1 | 0.0 | counted_fail | 1,501,027 / 27,897 | 62 | LoopBreakStop | 45 -> 60 | trajectory_truncated 62/62 |
| 001897 | 2 | har120-001897-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002104 | 1 | har120-002104-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002104 | 1 | har120-002104-a1-r2 | 0.0 | counted_fail | 2,413,962 / 17,295 | 85 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 85/85 |
| 002104 | 2 | har120-002104-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002356 | 1 | har120-002356-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002356 | 1 | har120-002356-a1-r2 | 1.0 | excluded:copied_fix+pass_tainted | 1,592,090 / 11,918 | 73 | LoopBreakStop | 58 -> 73 | trajectory_truncated 73/73 |
| 002356 | 2 | har120-002356-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002393 | 1 | har120-002393-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002393 | 1 | har120-002393-a1-r2 | 0.0 | counted_fail | 275,421 / 3,705 | 40 | LoopBreakStop | 25 -> 40 | trajectory_truncated 40/40 |
| 002393 | 2 | har120-002393-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002416 | 1 | har120-002416-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002416 | 1 | har120-002416-a1-r2 | 1.0 | counted_pass | 2,265,118 / 13,468 | 120 | TrialBudgetExhausted (request ceiling) | - | trajectory_truncated 120/120 |
| 002416 | 2 | har120-002416-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002552 | 1 | har120-002552-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002552 | 1 | har120-002552-a1-r2 | 1.0 | counted_pass | 2,464,413 / 11,815 | 95 | TrialBudgetExhausted (input-token ceiling) | nudge 59 (no stop) | trajectory_truncated 95/95 |
| 002552 | 2 | har120-002552-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002555 | 1 | har120-002555-a1 | None | infra | 0 / 0 | 27 | infra (upstream 503) | - | complete 27/27 |
| 002555 | 1 | har120-002555-a1-r2 | 1.0 | counted_pass | 1,490,570 / 15,680 | 65 | LoopBreakStop | 50 -> 65 | trajectory_truncated 65/65 |
| 002555 | 2 | har120-002555-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002680 | 1 | har120-002680-a1 | 0.0 | counted_fail | 2,458,142 / 17,377 | 89 | TrialBudgetExhausted (input-token ceiling) | - | trajectory_truncated 89/89 |
| 002680 | 2 | har120-002680-a2 | None | pending | - / - | - | absent | - | refused/absent |
| 002938 | 1 | har120-002938-a1 | 1.0 | counted_pass | 485,421 / 4,929 | 51 | LoopBreakStop | 36 -> 51 | trajectory_truncated 51/51 |
| 002938 | 2 | har120-002938-a2 | None | pending | - / - | - | absent | - | refused/absent |

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
Capture g2: 4306 calls, upstream latency p50=1.59s p90=4.9s, errors=0, status={'200': 3712, '503': 594}.
Capture g2b: 0 calls (capture file empty).
Capture g2c: 0 calls (capture file empty).

### Infra note (HAR-129)

- Tick-boundary teardown gap: tick 1's teardown stopped the Modal app at 09:02Z even though 40 re-estimated specs were approved; the redeploy's warm smoke returned HTTP 503 while cold-starting, the round script did not abort, and 22 dispatched specs ended ServiceUnavailableError with 0 tokens (plus 18 quiet-failure refusals). Cleared by the Q1 $0 local-Docker nop; r2 redeployed with a must-pass warm check.
- Wave (non-refill) dispatch: within tick 1, queue `waiting` held 39 refused specs while `running` drained 20 -> 2, so no mid-tick refill was observable; whether a tick refills freed slots when dispatchable specs exist is untested [INFERENCE]. SGLang served ~2 running requests at 153-250 tok/s at the tail vs ~400 tok/s under full 20-way load.

## 7. Capture-link status

- har120-000227-a1: assigned=99 unassigned=4196 ambiguous=0 verdict=trajectory_truncated.
- har120-000227-a2: assigned=27 unassigned=4268 ambiguous=0 verdict=complete.
- har120-000227-a2-r2: assigned=96 unassigned=4199 ambiguous=0 verdict=trajectory_truncated.
- har120-000341-a1: assigned=88 unassigned=4207 ambiguous=0 verdict=trajectory_truncated.
- har120-000341-a2: assigned=27 unassigned=4268 ambiguous=0 verdict=complete.
- har120-000341-a2-r2: assigned=96 unassigned=4199 ambiguous=0 verdict=trajectory_truncated.
- har120-000552-a1: assigned=104 unassigned=4192 ambiguous=0 verdict=trajectory_truncated.
- har120-000552-a2: assigned=27 unassigned=4269 ambiguous=0 verdict=complete.
- har120-000552-a2-r2: assigned=54 unassigned=4242 ambiguous=0 verdict=trajectory_truncated.
- har120-000666-a1: assigned=27 unassigned=4269 ambiguous=0 verdict=trajectory_truncated.
- har120-000666-a2: assigned=27 unassigned=4269 ambiguous=0 verdict=complete.
- har120-000666-a2-r2: assigned=59 unassigned=4237 ambiguous=0 verdict=trajectory_truncated.
- har120-000803-a1: assigned=42 unassigned=4254 ambiguous=0 verdict=trajectory_truncated.
- har120-000803-a2: assigned=27 unassigned=4269 ambiguous=0 verdict=complete.
- har120-000803-a2-r2: assigned=107 unassigned=4189 ambiguous=0 verdict=trajectory_truncated.
- har120-000813-a1: assigned=69 unassigned=4227 ambiguous=0 verdict=trajectory_truncated.
- har120-000813-a2: assigned=27 unassigned=4269 ambiguous=0 verdict=complete.
- har120-000813-a2-r2: assigned=103 unassigned=4193 ambiguous=0 verdict=trajectory_truncated.
- har120-000838-a1: assigned=83 unassigned=4213 ambiguous=0 verdict=trajectory_truncated.
- har120-000838-a2: assigned=27 unassigned=4269 ambiguous=0 verdict=complete.
- har120-000838-a2-r2: assigned=81 unassigned=4215 ambiguous=0 verdict=trajectory_truncated.
- har120-000865-a1: assigned=33 unassigned=4264 ambiguous=0 verdict=trajectory_truncated.
- har120-000865-a2: assigned=27 unassigned=4270 ambiguous=0 verdict=complete.
- har120-000865-a2-r2: assigned=66 unassigned=4231 ambiguous=0 verdict=trajectory_truncated.
- har120-000941-a1: assigned=102 unassigned=4195 ambiguous=0 verdict=trajectory_truncated.
- har120-000941-a2: assigned=27 unassigned=4270 ambiguous=0 verdict=complete.
- har120-000941-a2-r2: assigned=73 unassigned=4224 ambiguous=0 verdict=trajectory_truncated.
- har120-001265-a1: assigned=60 unassigned=4237 ambiguous=0 verdict=trajectory_truncated.
- har120-001265-a2: assigned=27 unassigned=4270 ambiguous=0 verdict=complete.
- har120-001265-a2-r2: assigned=98 unassigned=4199 ambiguous=0 verdict=trajectory_truncated.
- har120-001269-a1: assigned=92 unassigned=4205 ambiguous=0 verdict=trajectory_truncated.
- har120-001269-a2: assigned=27 unassigned=4270 ambiguous=0 verdict=complete.
- har120-001269-a2-r2: assigned=101 unassigned=4196 ambiguous=0 verdict=trajectory_truncated.
- har120-001373-a1: assigned=71 unassigned=4226 ambiguous=0 verdict=trajectory_truncated.
- har120-001373-a2: assigned=27 unassigned=4271 ambiguous=0 verdict=complete.
- har120-001373-a2-r2: assigned=30 unassigned=4268 ambiguous=0 verdict=trajectory_truncated.
- har120-001399-a1: assigned=87 unassigned=4211 ambiguous=0 verdict=trajectory_truncated.
- har120-001609-a1: assigned=27 unassigned=4271 ambiguous=0 verdict=complete.
- har120-001609-a1-r2: assigned=75 unassigned=4223 ambiguous=0 verdict=trajectory_truncated.
- har120-001618-a1: assigned=27 unassigned=4271 ambiguous=0 verdict=complete.
- har120-001618-a1-r2: assigned=79 unassigned=4219 ambiguous=0 verdict=trajectory_truncated.
- har120-001647-a1: assigned=0 unassigned=4298 ambiguous=0 verdict=capture_missing.
- har120-001661-a1: assigned=56 unassigned=4242 ambiguous=0 verdict=trajectory_truncated.
- har120-001710-a1: assigned=94 unassigned=4204 ambiguous=0 verdict=trajectory_truncated.
- har120-001820-a1: assigned=27 unassigned=4271 ambiguous=0 verdict=complete.
- har120-001820-a1-r2: assigned=32 unassigned=4266 ambiguous=0 verdict=trajectory_truncated.
- har120-001865-a1: assigned=27 unassigned=4272 ambiguous=0 verdict=complete.
- har120-001865-a1-r2: assigned=59 unassigned=4240 ambiguous=0 verdict=trajectory_truncated.
- har120-001870-a1: assigned=120 unassigned=4179 ambiguous=0 verdict=trajectory_truncated.
- har120-001897-a1: assigned=62 unassigned=4237 ambiguous=0 verdict=trajectory_truncated.
- har120-002104-a1: assigned=27 unassigned=4272 ambiguous=0 verdict=complete.
- har120-002104-a1-r2: assigned=85 unassigned=4214 ambiguous=0 verdict=trajectory_truncated.
- har120-002356-a1: assigned=27 unassigned=4272 ambiguous=0 verdict=complete.
- har120-002356-a1-r2: assigned=73 unassigned=4226 ambiguous=0 verdict=trajectory_truncated.
- har120-002393-a1: assigned=27 unassigned=4272 ambiguous=0 verdict=complete.
- har120-002393-a1-r2: assigned=40 unassigned=4259 ambiguous=0 verdict=trajectory_truncated.
- har120-002416-a1: assigned=27 unassigned=4272 ambiguous=0 verdict=complete.
- har120-002416-a1-r2: assigned=120 unassigned=4179 ambiguous=0 verdict=trajectory_truncated.
- har120-002552-a1: assigned=27 unassigned=4273 ambiguous=0 verdict=complete.
- har120-002552-a1-r2: assigned=95 unassigned=4205 ambiguous=0 verdict=trajectory_truncated.
- har120-002555-a1: assigned=27 unassigned=4273 ambiguous=0 verdict=complete.
- har120-002555-a1-r2: assigned=65 unassigned=4235 ambiguous=0 verdict=trajectory_truncated.
- har120-002680-a1: assigned=89 unassigned=4211 ambiguous=0 verdict=trajectory_truncated.
- har120-002938-a1: assigned=51 unassigned=4249 ambiguous=0 verdict=trajectory_truncated.

Linked 64 finished jobs, 0 without receipt.

