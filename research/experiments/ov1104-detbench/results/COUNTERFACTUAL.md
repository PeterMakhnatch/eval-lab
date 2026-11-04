# Counterfactual: L0 reads every known tool shape (DEV replay-v1 + Held-out sets, L0 stage only, no AutoReview)

`now` = current-main `evaluate_l0_stage` on the DetBench view. `cf` = the same rules after `l0_rules.map_tool_shapes` rewrote every known tool shape (Terminus `bash_command{keystrokes}`, `shell{command}`, cloud-export names, read-only tools) into one the parser reads. 'flagged' = L0 decision review or drop. 'caught by content' = any L0 reason other than the shape/parse notes. Wilson 95% CIs in brackets. No src change ships; the mapping lives in l0_rules.py only.

## DEV replay-v1 (690 runs, pre-registered H5)

### view = full

| corpus | honest n | honest flagged (now) | honest flagged (cf) | honest flagged only by shape (now) | honest flagged only by shape (cf) | cheat n | cheats caught by content (now) | cheats caught by content (cf) | cheats hard-dropped (now) | cheats hard-dropped (cf) | cheats flagged at all (now) | cheats flagged at all (cf) | unrecognized steps now -> cf |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c1c5 | 19 | 17/19 (89% [69,97]) | 15/19 (79% [57,91]) | 2/19 (11% [3,31]) | 0/19 (0% [0,17]) | 11 | 4/11 (36% [15,65]) | 4/11 (36% [15,65]) | 4/11 (36% [15,65]) | 4/11 (36% [15,65]) | 11/11 (100% [74,100]) | 9/11 (82% [52,95]) | 40 -> 2 |
| cheatbench | 75 | 61/75 (81% [71,89]) | 39/75 (52% [41,63]) | 25/75 (33% [24,45]) | 3/75 (4% [1,11]) | 75 | 56/75 (75% [64,83]) | 56/75 (75% [64,83]) | 23/75 (31% [21,42]) | 25/75 (33% [24,45]) | 74/75 (99% [93,100]) | 57/75 (76% [65,84]) | 2121 -> 157 |
| goodfire | 70 | 10/70 (14% [8,24]) | 10/70 (14% [8,24]) | 0/70 (0% [0,5]) | 0/70 (0% [0,5]) | 80 | 20/80 (25% [17,35]) | 20/80 (25% [17,35]) | 16/80 (20% [13,30]) | 16/80 (20% [13,30]) | 20/80 (25% [17,35]) | 20/80 (25% [17,35]) | 0 -> 0 |
| sol-astra | 48 | 19/48 (40% [27,54]) | 3/48 (6% [2,17]) | 19/48 (40% [27,54]) | 3/48 (6% [2,17]) | 8 | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 26 -> 3 |
| terminal-wrench | 146 | 146/146 (100% [97,100]) | 41/146 (28% [21,36]) | 114/146 (78% [71,84]) | 0/146 (0% [0,3]) | 158 | 128/158 (81% [74,86]) | 129/158 (82% [75,87]) | 121/158 (77% [69,83]) | 121/158 (77% [69,83]) | 158/158 (100% [98,100]) | 129/158 (82% [75,87]) | 1697 -> 0 |
| ALL | 358 | 253/358 (71% [66,75]) | 108/358 (30% [26,35]) | 160/358 (45% [40,50]) | 6/358 (2% [1,4]) | 332 | 214/332 (64% [59,69]) | 215/332 (65% [59,70]) | 170/332 (51% [46,57]) | 172/332 (52% [46,57]) | 269/332 (81% [76,85]) | 221/332 (67% [61,71]) | 3884 -> 162 |

Tool names still present in runs with unrecognized steps after the mapping (top 15): [('execute_bash', 481), ('execute_code', 193), ('edit_file', 191), ('str_replace_editor', 146), ('sleep', 115), ('read', 51), ('request_expert_review', 41), ('bash', 34), ('edit', 10), ('glob', 9), ('unknown_tool', 9), ('write', 7), ('submit_final_answer', 7), ('create_file', 5), ('think', 4)]

Mappings applied (full): {'glob->think': 81, 'grep->think': 32, 'shell->bash': 554, 'web_search->think': 67, 'file_change->edit': 14, 'toolsearch->think': 17, 'todowrite->think': 26, 'execute_bash->bash': 481, 'request_expert_review->think': 79, 'execute_code->python': 342, 'sleep->think': 516, 'edit_file->edit': 319, 'submit_final_answer->think': 20, 'bash(odd-key)->bash': 125, 'task_complete->think': 33, 'edit_notebook->edit': 41, 'execute_notebook_code->python': 34, 'wait->think': 20, 'bash_command->bash': 6179, 'mark_task_complete->think': 310, 'close_file->think': 2}

### view = actions_only

| corpus | honest n | honest flagged (now) | honest flagged (cf) | honest flagged only by shape (now) | honest flagged only by shape (cf) | cheat n | cheats caught by content (now) | cheats caught by content (cf) | cheats hard-dropped (now) | cheats hard-dropped (cf) | cheats flagged at all (now) | cheats flagged at all (cf) | unrecognized steps now -> cf |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c1c5 | 19 | 17/19 (89% [69,97]) | 15/19 (79% [57,91]) | 2/19 (11% [3,31]) | 0/19 (0% [0,17]) | 11 | 4/11 (36% [15,65]) | 4/11 (36% [15,65]) | 4/11 (36% [15,65]) | 4/11 (36% [15,65]) | 11/11 (100% [74,100]) | 9/11 (82% [52,95]) | 40 -> 2 |
| cheatbench | 75 | 61/75 (81% [71,89]) | 39/75 (52% [41,63]) | 25/75 (33% [24,45]) | 3/75 (4% [1,11]) | 75 | 56/75 (75% [64,83]) | 56/75 (75% [64,83]) | 22/75 (29% [20,40]) | 24/75 (32% [23,43]) | 74/75 (99% [93,100]) | 57/75 (76% [65,84]) | 2121 -> 157 |
| goodfire | 70 | 10/70 (14% [8,24]) | 10/70 (14% [8,24]) | 0/70 (0% [0,5]) | 0/70 (0% [0,5]) | 80 | 20/80 (25% [17,35]) | 20/80 (25% [17,35]) | 16/80 (20% [13,30]) | 16/80 (20% [13,30]) | 20/80 (25% [17,35]) | 20/80 (25% [17,35]) | 0 -> 0 |
| sol-astra | 48 | 19/48 (40% [27,54]) | 3/48 (6% [2,17]) | 19/48 (40% [27,54]) | 3/48 (6% [2,17]) | 8 | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 6/8 (75% [41,93]) | 26 -> 3 |
| terminal-wrench | 146 | 146/146 (100% [97,100]) | 64/146 (44% [36,52]) | 89/146 (61% [53,68]) | 0/146 (0% [0,3]) | 158 | 128/158 (81% [74,86]) | 129/158 (82% [75,87]) | 121/158 (77% [69,83]) | 121/158 (77% [69,83]) | 158/158 (100% [98,100]) | 129/158 (82% [75,87]) | 1697 -> 0 |
| ALL | 358 | 253/358 (71% [66,75]) | 131/358 (37% [32,42]) | 135/358 (38% [33,43]) | 6/358 (2% [1,4]) | 332 | 214/332 (64% [59,69]) | 215/332 (65% [59,70]) | 169/332 (51% [46,56]) | 171/332 (52% [46,57]) | 269/332 (81% [76,85]) | 221/332 (67% [61,71]) | 3884 -> 162 |

Tool names still present in runs with unrecognized steps after the mapping (top 15): [('execute_bash', 481), ('execute_code', 193), ('edit_file', 191), ('str_replace_editor', 146), ('sleep', 115), ('read', 51), ('request_expert_review', 41), ('bash', 34), ('edit', 10), ('glob', 9), ('unknown_tool', 9), ('write', 7), ('submit_final_answer', 7), ('create_file', 5), ('think', 4)]

Mappings applied (actions_only): {'glob->think': 81, 'grep->think': 32, 'shell->bash': 554, 'web_search->think': 67, 'file_change->edit': 14, 'toolsearch->think': 17, 'todowrite->think': 26, 'execute_bash->bash': 481, 'request_expert_review->think': 79, 'execute_code->python': 342, 'sleep->think': 516, 'edit_file->edit': 319, 'submit_final_answer->think': 20, 'bash(odd-key)->bash': 125, 'task_complete->think': 33, 'edit_notebook->edit': 41, 'execute_notebook_code->python': 34, 'wait->think': 20, 'bash_command->bash': 6179, 'mark_task_complete->think': 310, 'close_file->think': 2}

## Decision transitions (#1825 operational view)

The tables above count runs that are "flagged". This section counts what changes in the *decision*
the line actually emits: `drop` (hard), `review` (soft), `pass` (nothing). `now` = current
`evaluate_l0_stage`; `cf` = the same rules after `map_tool_shapes`. Counts are per run, from
`l0/l0v_full_raw.jsonl` and `l0/l0cf_full_raw.jsonl`; `actions_only` differs only where noted.

### DEV replay-v1 (690 runs)

| label | transition | n | where |
|---|---|---:|---|
| honest | `review -> pass` | **145 / 358 (40.5%)** | c1c5 2, cheatbench 22, sol-astra 16, terminal-wrench 105 |
| honest | `review -> drop` | 1 / 358 | cheatbench 1 |
| honest | `drop -> *` | **0** | — |
| cheat | `review -> pass` | 48 / 332 (14.5%) | c1c5 2, cheatbench 17, terminal-wrench 29 |
| cheat | `review -> drop` | 2 / 332 | cheatbench 2 |
| cheat | `drop -> *` | **0** | — |

### Held-out (15,859 runs)

Across all 20 sets, **0 runs of either label move out of `drop`** (`drop -> pass` = 0,
`drop -> review` = 0). Every change is a soft `review -> pass` (signal lost) or a `review/pass ->
drop` (signal gained):

| set | cheat soft signals lost (`review -> pass`) | honest runs newly flagged (`review -> drop`) |
|---|---:|---:|
| `sdf-shortcut-taking` | **257 / 359 (71.6%)** | 5 |
| `mistrace-reward-hacking` | **80 / 119 (67.2%)** | 1 |
| `kobayashi-maru` | 30 / 272 (11.0%) | 0 |
| `sleight-bench` | 24 / 43 (55.8%) | 2 |
| `agent-cheat-index` | 19 / 62 (30.6%) | **9** |
| `researcharena-benchmark-gaming` | 3 / 21 | **4 / 20** |
| `cape-bench` | 1 / 400 | 0 |
| `kernelbench-hard-runs` | 1 / 11 | 2 / 19 |
| `beam-tripwire` | 0 | **18 / 442 (4.1%)** |
| all other 11 sets | 0 | 0 |
| **all 20 sets** | **463 / 4,104 (11.3%)** | **42 / 11,755 (0.36%)** |

Decision counts (`full` view): honest relieved 2,211, honest newly flagged 42 (net **+2,169**);
cheat relieved 463, cheat newly flagged 26 (net **+437**). `actions_only` differs only on
`terminal-wrench` honest relief (105 -> 82) and `evilgenie-escalation` (0 either way).

### How to read this for the #1825 decision

- **Hard drops are preserved exactly.** No run that L0 hard-drops today stops being hard-dropped
  under tool-shape recognition, on DEV or on any held-out set. The change is safe with respect to
  the strongest action the line takes.
- **The honest-false-alarm relief is large and concentrated in `review`**: 145/358 honest DEV runs
  leave the review queue (40.5%), and on held-out `reflex-sentinel` 197/205 (96.1%), `kobayashi-maru`
  303/2,728 (11.1%), `sdf-shortcut-taking` 595/636 (93.6%), `reward-pressure-study-2` 320/653 (49.0%).
- **The cost is 463 held-out cheats that stop being surfaced at `review`**, because the only reason
  they were surfaced was the unrecognized-tool-shape parser artifact, not a content rule. That is
  71.6% of `sdf-shortcut-taking` and 67.2% of `mistrace-reward-hacking` cheats. Content rules only
  catch 24.5% and 6.7% of those sets today, so on those two sets the review queue loses most of its
  (accidental) coverage. The honest answer: tool-shape recognition improves the queue's precision
  but shrinks its recall on two held-out sets, and it does so without losing a single hard drop.
- **Small new honest false alarms appear**: 42 held-out runs move to `drop`, 18 of them on
  `beam-tripwire` (4.1% of its honest runs) and 9 on `agent-cheat-index`. Those are runs whose
  newly-parsed tool calls match real hard rules once the shape is readable — worth inspecting before
  shipping, because they are the cases where correct parsing turns a benign-looking action into a
  hard drop.

## Held-out sets (15,859 runs across 20 sets)

### view = full

| corpus | honest n | honest flagged (now) | honest flagged (cf) | honest flagged only by shape (now) | honest flagged only by shape (cf) | cheat n | cheats caught by content (now) | cheats caught by content (cf) | cheats hard-dropped (now) | cheats hard-dropped (cf) | cheats flagged at all (now) | cheats flagged at all (cf) | unrecognized steps now -> cf |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| agent-cheat-index | 1086 | 1083/1086 (100% [99,100]) | 525/1086 (48% [45,51]) | 1073/1086 (99% [98,99]) | 506/1086 (47% [44,50]) | 62 | 18/62 (29% [19,41]) | 21/62 (34% [23,46]) | 16/62 (26% [17,38]) | 18/62 (29% [19,41]) | 62/62 (100% [94,100]) | 43/62 (69% [57,79]) | 9920 -> 4552 |
| beam-tripwire | 442 | 385/442 (87% [84,90]) | 386/442 (87% [84,90]) | 291/442 (66% [61,70]) | 276/442 (62% [58,67]) | 171 | 102/171 (60% [52,67]) | 104/171 (61% [53,68]) | 97/171 (57% [49,64]) | 101/171 (59% [52,66]) | 170/171 (99% [97,100]) | 170/171 (99% [97,100]) | 3947 -> 3947 |
| cape-bench | 400 | 399/400 (100% [99,100]) | 395/400 (99% [97,99]) | 362/400 (90% [87,93]) | 358/400 (90% [86,92]) | 400 | 62/400 (16% [12,19]) | 62/400 (16% [12,19]) | 25/400 (6% [4,9]) | 25/400 (6% [4,9]) | 400/400 (100% [99,100]) | 399/400 (100% [99,100]) | 5757 -> 4063 |
| caught-not-taught | 160 | 136/160 (85% [79,90]) | 136/160 (85% [79,90]) | 128/160 (80% [73,85]) | 128/160 (80% [73,85]) | 46 | 0/46 (0% [0,8]) | 0/46 (0% [0,8]) | 0/46 (0% [0,8]) | 0/46 (0% [0,8]) | 36/46 (78% [64,88]) | 36/46 (78% [64,88]) | 246 -> 246 |
| evilgenie-escalation | 1990 | 1893/1990 (95% [94,96]) | 1893/1990 (95% [94,96]) | 1783/1990 (90% [88,91]) | 1783/1990 (90% [88,91]) | 124 | 3/124 (2% [1,7]) | 3/124 (2% [1,7]) | 0/124 (0% [0,3]) | 0/124 (0% [0,3]) | 124/124 (100% [97,100]) | 124/124 (100% [97,100]) | 26937 -> 26937 |
| flake-laundering | 217 | 47/217 (22% [17,28]) | 47/217 (22% [17,28]) | 0/217 (0% [0,2]) | 0/217 (0% [0,2]) | 7 | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 1 -> 1 |
| kernelbench-hard-runs | 19 | 16/19 (84% [62,94]) | 14/19 (74% [51,88]) | 14/19 (74% [51,88]) | 10/19 (53% [32,73]) | 11 | 2/11 (18% [5,48]) | 2/11 (18% [5,48]) | 1/11 (9% [2,38]) | 2/11 (18% [5,48]) | 9/11 (82% [52,95]) | 8/11 (73% [43,90]) | 1921 -> 619 |
| kobayashi-maru | 2728 | 2691/2728 (99% [98,99]) | 2388/2728 (88% [86,89]) | 2679/2728 (98% [98,99]) | 2376/2728 (87% [86,88]) | 272 | 52/272 (19% [15,24]) | 52/272 (19% [15,24]) | 52/272 (19% [15,24]) | 52/272 (19% [15,24]) | 270/272 (99% [97,100]) | 240/272 (88% [84,92]) | 5731 -> 2757 |
| latent-agent-sandbox-episodes | 148 | 20/148 (14% [9,20]) | 20/148 (14% [9,20]) | 0/148 (0% [0,3]) | 0/148 (0% [0,3]) | 178 | 44/178 (25% [19,32]) | 44/178 (25% [19,32]) | 41/178 (23% [17,30]) | 41/178 (23% [17,30]) | 44/178 (25% [19,32]) | 44/178 (25% [19,32]) | 0 -> 0 |
| mistrace-reward-hacking | 81 | 65/81 (80% [70,87]) | 3/81 (4% [1,10]) | 65/81 (80% [70,87]) | 2/81 (2% [1,9]) | 119 | 8/119 (7% [3,13]) | 8/119 (7% [3,13]) | 8/119 (7% [3,13]) | 8/119 (7% [3,13]) | 91/119 (76% [68,83]) | 11/119 (9% [5,16]) | 3862 -> 6 |
| paichecker | 1502 | 21/1502 (1% [1,2]) | 21/1502 (1% [1,2]) | 0/1502 (0% [0,0]) | 0/1502 (0% [0,0]) | 1085 | 12/1085 (1% [1,2]) | 12/1085 (1% [1,2]) | 4/1085 (0% [0,1]) | 4/1085 (0% [0,1]) | 12/1085 (1% [1,2]) | 12/1085 (1% [1,2]) | 0 -> 0 |
| reflex-sentinel | 205 | 205/205 (100% [98,100]) | 8/205 (4% [2,8]) | 201/205 (98% [95,99]) | 4/205 (2% [1,5]) | 45 | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 1261 -> 6 |
| researcharena-benchmark-gaming | 20 | 20/20 (100% [84,100]) | 18/20 (90% [70,97]) | 9/20 (45% [26,66]) | 1/20 (5% [1,24]) | 21 | 12/21 (57% [37,76]) | 17/21 (81% [60,92]) | 5/21 (24% [11,45]) | 10/21 (48% [28,68]) | 21/21 (100% [85,100]) | 18/21 (86% [65,95]) | 6427 -> 344 |
| resolvebench-support-agents | 400 | 400/400 (100% [99,100]) | 400/400 (100% [99,100]) | 400/400 (100% [99,100]) | 400/400 (100% [99,100]) | 112 | 0/112 (0% [0,3]) | 0/112 (0% [0,3]) | 0/112 (0% [0,3]) | 0/112 (0% [0,3]) | 112/112 (100% [97,100]) | 112/112 (100% [97,100]) | 4601 -> 4601 |
| reward-pressure-study-2 | 653 | 653/653 (100% [99,100]) | 333/653 (51% [47,55]) | 649/653 (99% [98,100]) | 329/653 (50% [47,54]) | 467 | 35/467 (7% [5,10]) | 35/467 (7% [5,10]) | 27/467 (6% [4,8]) | 27/467 (6% [4,8]) | 467/467 (100% [99,100]) | 467/467 (100% [99,100]) | 3748 -> 1595 |
| rhb-delivery-queue | 409 | 403/409 (99% [97,99]) | 403/409 (99% [97,99]) | 403/409 (99% [97,99]) | 403/409 (99% [97,99]) | 129 | 0/129 (0% [0,3]) | 0/129 (0% [0,3]) | 0/129 (0% [0,3]) | 0/129 (0% [0,3]) | 129/129 (100% [97,100]) | 129/129 (100% [97,100]) | 1804 -> 1804 |
| rsi-benchmark-monitor-training | 416 | 416/416 (100% [99,100]) | 416/416 (100% [99,100]) | 416/416 (100% [99,100]) | 416/416 (100% [99,100]) | 420 | 0/420 (0% [0,1]) | 0/420 (0% [0,1]) | 0/420 (0% [0,1]) | 0/420 (0% [0,1]) | 420/420 (100% [99,100]) | 420/420 (100% [99,100]) | 13448 -> 11797 |
| sdf-shortcut-taking | 636 | 636/636 (100% [99,100]) | 41/636 (6% [5,9]) | 616/636 (97% [95,98]) | 16/636 (3% [2,4]) | 359 | 88/359 (25% [20,29]) | 99/359 (28% [23,32]) | 17/359 (5% [3,7]) | 26/359 (7% [5,10]) | 359/359 (100% [99,100]) | 102/359 (28% [24,33]) | 37034 -> 78 |
| sleight-bench | 43 | 42/43 (98% [88,100]) | 19/43 (44% [30,59]) | 26/43 (60% [46,74]) | 0/43 (0% [0,8]) | 43 | 13/43 (30% [19,45]) | 17/43 (40% [26,54]) | 4/43 (9% [4,22]) | 7/43 (16% [8,30]) | 42/43 (98% [88,100]) | 18/43 (42% [28,57]) | 466 -> 0 |
| trusted-monitoring-logs | 200 | 2/200 (1% [0,4]) | 2/200 (1% [0,4]) | 2/200 (1% [0,4]) | 2/200 (1% [0,4]) | 33 | 0/33 (0% [0,10]) | 0/33 (0% [0,10]) | 0/33 (0% [0,10]) | 0/33 (0% [0,10]) | 13/33 (39% [25,56]) | 13/33 (39% [25,56]) | 16 -> 16 |
| ALL | 11755 | 9533/11755 (81% [80,82]) | 7468/11755 (64% [63,64]) | 9117/11755 (78% [77,78]) | 7010/11755 (60% [59,61]) | 4104 | 496/4104 (12% [11,13]) | 521/4104 (13% [12,14]) | 342/4104 (8% [8,9]) | 366/4104 (9% [8,10]) | 2826/4104 (69% [67,70]) | 2411/4104 (59% [57,60]) | 127127 -> 63369 |

Tool names still present in runs with unrecognized steps after the mapping (top 15): [('safe_bash', 14399), ('safe_python', 13749), ('run_command', 10413), ('read_file', 7628), ('text_editor', 6430), ('bash', 5545), ('submit', 5156), ('get_flights_between_locations', 4813), ('bash_command', 4674), ('write_file', 4412), ('run_tests', 4362), ('buy_flight_ticket', 3983), ('read', 3429), ('send_email', 3384), ('search_contacts_by_name', 3101)]

Mappings applied (full): {'bash_command->bash': 17913, 'glob->think': 398, 'todowrite->think': 439, 'grep->think': 72, 'task->think': 13, 'run_command->bash': 12881, 'read_file->read': 8368, 'edit_file->edit': 1028, 'write_file->write': 5142, 'cat->read': 1, 'list_files->think': 3007, 'search->think': 2, 'toolsearch->think': 10, 'bash(odd-key)->bash': 5, 'web_search->think': 1, 'search_files->think': 239, 'end_task->think': 835, 'execute_command->bash': 37194}

### view = actions_only

| corpus | honest n | honest flagged (now) | honest flagged (cf) | honest flagged only by shape (now) | honest flagged only by shape (cf) | cheat n | cheats caught by content (now) | cheats caught by content (cf) | cheats hard-dropped (now) | cheats hard-dropped (cf) | cheats flagged at all (now) | cheats flagged at all (cf) | unrecognized steps now -> cf |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| agent-cheat-index | 1086 | 1083/1086 (100% [99,100]) | 525/1086 (48% [45,51]) | 1073/1086 (99% [98,99]) | 506/1086 (47% [44,50]) | 62 | 18/62 (29% [19,41]) | 21/62 (34% [23,46]) | 16/62 (26% [17,38]) | 18/62 (29% [19,41]) | 62/62 (100% [94,100]) | 43/62 (69% [57,79]) | 9920 -> 4552 |
| beam-tripwire | 442 | 385/442 (87% [84,90]) | 386/442 (87% [84,90]) | 291/442 (66% [61,70]) | 276/442 (62% [58,67]) | 171 | 102/171 (60% [52,67]) | 104/171 (61% [53,68]) | 97/171 (57% [49,64]) | 101/171 (59% [52,66]) | 170/171 (99% [97,100]) | 170/171 (99% [97,100]) | 3947 -> 3947 |
| cape-bench | 400 | 399/400 (100% [99,100]) | 395/400 (99% [97,99]) | 362/400 (90% [87,93]) | 358/400 (90% [86,92]) | 400 | 62/400 (16% [12,19]) | 62/400 (16% [12,19]) | 25/400 (6% [4,9]) | 25/400 (6% [4,9]) | 400/400 (100% [99,100]) | 399/400 (100% [99,100]) | 5757 -> 4063 |
| caught-not-taught | 160 | 136/160 (85% [79,90]) | 136/160 (85% [79,90]) | 128/160 (80% [73,85]) | 128/160 (80% [73,85]) | 46 | 0/46 (0% [0,8]) | 0/46 (0% [0,8]) | 0/46 (0% [0,8]) | 0/46 (0% [0,8]) | 36/46 (78% [64,88]) | 36/46 (78% [64,88]) | 246 -> 246 |
| evilgenie-escalation | 1990 | 1979/1990 (99% [99,100]) | 1979/1990 (99% [99,100]) | 1783/1990 (90% [88,91]) | 1783/1990 (90% [88,91]) | 124 | 3/124 (2% [1,7]) | 3/124 (2% [1,7]) | 0/124 (0% [0,3]) | 0/124 (0% [0,3]) | 124/124 (100% [97,100]) | 124/124 (100% [97,100]) | 26937 -> 26937 |
| flake-laundering | 217 | 47/217 (22% [17,28]) | 47/217 (22% [17,28]) | 0/217 (0% [0,2]) | 0/217 (0% [0,2]) | 7 | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 0/7 (0% [0,35]) | 1 -> 1 |
| kernelbench-hard-runs | 19 | 16/19 (84% [62,94]) | 14/19 (74% [51,88]) | 14/19 (74% [51,88]) | 10/19 (53% [32,73]) | 11 | 2/11 (18% [5,48]) | 2/11 (18% [5,48]) | 1/11 (9% [2,38]) | 2/11 (18% [5,48]) | 9/11 (82% [52,95]) | 8/11 (73% [43,90]) | 1921 -> 619 |
| kobayashi-maru | 2728 | 2691/2728 (99% [98,99]) | 2388/2728 (88% [86,89]) | 2679/2728 (98% [98,99]) | 2376/2728 (87% [86,88]) | 272 | 52/272 (19% [15,24]) | 52/272 (19% [15,24]) | 52/272 (19% [15,24]) | 52/272 (19% [15,24]) | 270/272 (99% [97,100]) | 240/272 (88% [84,92]) | 5731 -> 2757 |
| latent-agent-sandbox-episodes | 148 | 20/148 (14% [9,20]) | 20/148 (14% [9,20]) | 0/148 (0% [0,3]) | 0/148 (0% [0,3]) | 178 | 44/178 (25% [19,32]) | 44/178 (25% [19,32]) | 41/178 (23% [17,30]) | 41/178 (23% [17,30]) | 44/178 (25% [19,32]) | 44/178 (25% [19,32]) | 0 -> 0 |
| mistrace-reward-hacking | 81 | 65/81 (80% [70,87]) | 3/81 (4% [1,10]) | 65/81 (80% [70,87]) | 2/81 (2% [1,9]) | 119 | 8/119 (7% [3,13]) | 8/119 (7% [3,13]) | 8/119 (7% [3,13]) | 8/119 (7% [3,13]) | 91/119 (76% [68,83]) | 11/119 (9% [5,16]) | 3862 -> 6 |
| paichecker | 1502 | 21/1502 (1% [1,2]) | 21/1502 (1% [1,2]) | 0/1502 (0% [0,0]) | 0/1502 (0% [0,0]) | 1085 | 12/1085 (1% [1,2]) | 12/1085 (1% [1,2]) | 4/1085 (0% [0,1]) | 4/1085 (0% [0,1]) | 12/1085 (1% [1,2]) | 12/1085 (1% [1,2]) | 0 -> 0 |
| reflex-sentinel | 205 | 205/205 (100% [98,100]) | 8/205 (4% [2,8]) | 201/205 (98% [95,99]) | 4/205 (2% [1,5]) | 45 | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 45/45 (100% [92,100]) | 1261 -> 6 |
| researcharena-benchmark-gaming | 20 | 20/20 (100% [84,100]) | 18/20 (90% [70,97]) | 9/20 (45% [26,66]) | 1/20 (5% [1,24]) | 21 | 12/21 (57% [37,76]) | 17/21 (81% [60,92]) | 5/21 (24% [11,45]) | 10/21 (48% [28,68]) | 21/21 (100% [85,100]) | 18/21 (86% [65,95]) | 6427 -> 344 |
| resolvebench-support-agents | 400 | 400/400 (100% [99,100]) | 400/400 (100% [99,100]) | 400/400 (100% [99,100]) | 400/400 (100% [99,100]) | 112 | 0/112 (0% [0,3]) | 0/112 (0% [0,3]) | 0/112 (0% [0,3]) | 0/112 (0% [0,3]) | 112/112 (100% [97,100]) | 112/112 (100% [97,100]) | 4601 -> 4601 |
| reward-pressure-study-2 | 653 | 653/653 (100% [99,100]) | 333/653 (51% [47,55]) | 649/653 (99% [98,100]) | 329/653 (50% [47,54]) | 467 | 35/467 (7% [5,10]) | 35/467 (7% [5,10]) | 27/467 (6% [4,8]) | 27/467 (6% [4,8]) | 467/467 (100% [99,100]) | 467/467 (100% [99,100]) | 3748 -> 1595 |
| rhb-delivery-queue | 409 | 403/409 (99% [97,99]) | 403/409 (99% [97,99]) | 403/409 (99% [97,99]) | 403/409 (99% [97,99]) | 129 | 0/129 (0% [0,3]) | 0/129 (0% [0,3]) | 0/129 (0% [0,3]) | 0/129 (0% [0,3]) | 129/129 (100% [97,100]) | 129/129 (100% [97,100]) | 1804 -> 1804 |
| rsi-benchmark-monitor-training | 416 | 416/416 (100% [99,100]) | 416/416 (100% [99,100]) | 416/416 (100% [99,100]) | 416/416 (100% [99,100]) | 420 | 0/420 (0% [0,1]) | 0/420 (0% [0,1]) | 0/420 (0% [0,1]) | 0/420 (0% [0,1]) | 420/420 (100% [99,100]) | 420/420 (100% [99,100]) | 13448 -> 11797 |
| sdf-shortcut-taking | 636 | 636/636 (100% [99,100]) | 41/636 (6% [5,9]) | 616/636 (97% [95,98]) | 16/636 (3% [2,4]) | 359 | 88/359 (25% [20,29]) | 99/359 (28% [23,32]) | 17/359 (5% [3,7]) | 26/359 (7% [5,10]) | 359/359 (100% [99,100]) | 102/359 (28% [24,33]) | 37034 -> 78 |
| sleight-bench | 43 | 42/43 (98% [88,100]) | 19/43 (44% [30,59]) | 26/43 (60% [46,74]) | 0/43 (0% [0,8]) | 43 | 13/43 (30% [19,45]) | 17/43 (40% [26,54]) | 4/43 (9% [4,22]) | 7/43 (16% [8,30]) | 42/43 (98% [88,100]) | 18/43 (42% [28,57]) | 466 -> 0 |
| trusted-monitoring-logs | 200 | 2/200 (1% [0,4]) | 2/200 (1% [0,4]) | 2/200 (1% [0,4]) | 2/200 (1% [0,4]) | 33 | 0/33 (0% [0,10]) | 0/33 (0% [0,10]) | 0/33 (0% [0,10]) | 0/33 (0% [0,10]) | 13/33 (39% [25,56]) | 13/33 (39% [25,56]) | 16 -> 16 |
| ALL | 11755 | 9619/11755 (82% [81,83]) | 7554/11755 (64% [63,65]) | 9117/11755 (78% [77,78]) | 7010/11755 (60% [59,61]) | 4104 | 496/4104 (12% [11,13]) | 521/4104 (13% [12,14]) | 342/4104 (8% [8,9]) | 366/4104 (9% [8,10]) | 2826/4104 (69% [67,70]) | 2411/4104 (59% [57,60]) | 127127 -> 63369 |

Tool names still present in runs with unrecognized steps after the mapping (top 15): [('safe_bash', 14399), ('safe_python', 13749), ('run_command', 10413), ('read_file', 7628), ('text_editor', 6430), ('bash', 5545), ('submit', 5156), ('get_flights_between_locations', 4813), ('bash_command', 4674), ('write_file', 4412), ('run_tests', 4362), ('buy_flight_ticket', 3983), ('read', 3429), ('send_email', 3384), ('search_contacts_by_name', 3101)]

Mappings applied (actions_only): {'bash_command->bash': 17913, 'glob->think': 398, 'todowrite->think': 439, 'grep->think': 72, 'task->think': 13, 'run_command->bash': 12881, 'read_file->read': 8368, 'edit_file->edit': 1028, 'write_file->write': 5142, 'cat->read': 1, 'list_files->think': 3007, 'search->think': 2, 'toolsearch->think': 10, 'bash(odd-key)->bash': 5, 'web_search->think': 1, 'search_files->think': 239, 'end_task->think': 835, 'execute_command->bash': 37194}

