# probe-03 capabilities summary

_Inputs: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-18737, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-41330, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42485576, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42496599, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42514310, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42528228, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1271-media-games, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1559-ml-evaluation, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1634-software-databases, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1702-ml-inference, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1789-security-appsec, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-2684-security-appsec, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000240, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000434, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-001520, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-001645, /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-002537. Invocation: `capabilities.py /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-* --out-dir har81-wave-a --treatment-key-source har93:/Users/petermakhnatch/Developer/eval-lab/derived/parquet/external/task_catalog/trial_treatment.parquet --evallab-src /Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/src --nop-runs-dir /Users/petermakhnatch/Developer/eval-lab/.worktrees/mimo-ops/runs --commit-equivalent 7de1ce6e,48b787b5 --equivalence-source HAR-81 Research-Harbor decision 09:27Z --pass-tainted har81-p-d-candidate-2684-security-appsec --taint-source HAR-81 Research-Harbor decision 10:32Z (forbidden network pip install of stevedore at head#33) --hand-key validation/har81-wave-a.hand-key.jsonl --wedge-key validation/har99-wedge.hand-key.jsonl`._

## Tag x attribution (outcome-relevant failure)

| tag | attribution | trials |
| --- | --- | --- |
| completion | model | 8 (`har81-p-d-arvo-41330`, `har81-p-d-arvo-42485576`, `har81-p-d-arvo-42496599`, `har81-p-d-arvo-42514310`, `har81-p-d-arvo-42528228`, `har81-p-d-candidate-1634-software-databases__PaSTYBj`, `har81-p-d-format-code-task-000434__iT2yUa7`, `har81-p-d-format-code-task-001520__P9EP83h`) |
| completion | unclear | 1 (`har81-p-d-arvo-18737`) |
| environment | harness | 2 (`har81-p-d-candidate-1702-ml-inference__izs4jgG`, `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ`) |
| none | n/a | 3 (`har81-p-d-candidate-1559-ml-evaluation__bqBv49S`, `har81-p-d-candidate-2684-security-appsec__uwoAzn7`, `har81-p-d-format-code-task-000240__9KYGvwT`) |
| planning | model | 3 (`har81-p-d-candidate-1271-media-games__grehkae`, `har81-p-d-format-code-task-001645__bMN27md`, `har81-p-d-format-code-task-002537__7xmXPRq`) |

## Per-trial: first failure vs outcome-relevant failure

| trial | outcome | stop reason | first failure (recovered) | outcome-relevant failure | rule | evidence |
| --- | --- | --- | --- | --- | --- | --- |
| `har81-p-d-arvo-18737` | 0.0 (scored) | task_complete_confirmed | head#16 tool_use/model (R-TOOL-02, recovered=true) | head#18 completion/unclear | R-COMP-03 | head#18,head#19 |
| | | | | | note: | deliverable_not_in_instruction: verifier names ['PoC', 'submit', 'submit.sh'] but instruction.md (258 B) never mentions them |
| `har81-p-d-arvo-41330` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | head#32 completion/model | R-COMP-03 | head#30,head#32 |
| `har81-p-d-arvo-42485576` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | -- completion/model | R-COMP-03 | head#34 |
| | | | | | note: | error_recovery/model secondary (R-REC-02 wedged terminal): no_prompt after head#47 (`cd /home/agent/binary && mkdir -p /tmp/corpus && cp /tmp/tes`), 22 turns head#48-trajectory.cont-1.json#14 with no interrupt, prompt back after the command finished |
| `har81-p-d-arvo-42496599` | 0.0 (scored) | ceiling:input_tokens | head#14 tool_use/model (R-TOOL-02, recovered=true) | -- completion/model | R-COMP-03 | head#3,head#21,head#118 |
| | | | | | note: | error_recovery/model secondary: identical run head#21-head#118 |
| | | | | | note: | error_recovery/model secondary (R-REC-02 wedged terminal): no_prompt after head#11 (`cd /home/agent/binary && file dictionary_stream_round_trip &`), 106 turns head#12-head#118 with no interrupt, until run end |
| `har81-p-d-arvo-42514310` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | -- completion/model | R-COMP-03 | head#3,head#6 |
| | | | | | note: | error_recovery/model secondary (R-REC-02 wedged terminal): no_prompt after head#8 (`cd /home/agent/binary && timeout 120 ./example_dict_fuzzer /`), 11 turns head#9-head#19 with no interrupt, prompt back after the command finished |
| `har81-p-d-arvo-42528228` | 0.0 (scored) | ceiling:input_tokens | head#4 tool_use/model (R-TOOL-02, recovered=true) | head#15 completion/model | R-COMP-03 | trajectory.cont-1.json#6,head#14,head#15 |
| `har81-p-d-candidate-1271-media-games__grehkae` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | -- planning/model | R-PLAN-01 | head#40,head#94 |
| | | | | | note: | error_recovery/model secondary: identical run head#40-head#94 |
| `har81-p-d-candidate-1559-ml-evaluation__bqBv49S` | 1.0 (scored) | task_complete_confirmed | head#7 completion/harness (R-COMP-01, recovered=true) | -- none/n/a | R-NONE-01 | -- |
| `har81-p-d-candidate-1634-software-databases__PaSTYBj` | 0.0 (scored) | task_complete_confirmed | none (clean execution) | head#39 completion/model | R-COMP-02 | head#39,head#41,head#42 |
| | | | | | note: | source_text_assertion: failing verifier test asserts literal source strings (test_outputs.py::test_edge_source_modules_compile_and_forward); asserted literal 'def atomic(self, transaction_type=None, **kwargs):' is not in instruction.md; grader audit flag, not R-ENV-02 |
| `har81-p-d-candidate-1702-ml-inference__izs4jgG` | 0.0 (scored) | ceiling:input_tokens | head#2 environment/harness (R-ENV-02, recovered=false) | head#2 environment/harness | R-ENV-02 | head#2 |
| `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ` | 0.0 (scored) | agent_timeout | head#7 environment/harness (R-ENV-02, recovered=false) | head#7 environment/harness | R-ENV-02 | head#7 |
| | | | | | note: | environment wrestling: model steps head#12-head#24 (13 steps; no target-file edits) |
| `har81-p-d-candidate-2684-security-appsec__uwoAzn7` | 1.0 (scored, pass_tainted) | ceiling:input_tokens | head#2 tool_use/model (R-TOOL-02, recovered=true) | -- none/n/a | R-NONE-01 | head#38,head#69 |
| | | | | | note: | error_recovery/model secondary: identical run head#38-head#69 |
| | | | | | caveat: | env_remediated: pip install stevedore at head#33 (network) while nop fails on missing stevedore |
| | | | | | caveat: | parse_errors: 43 |
| | | | | | caveat: | loop_heavy: 66% of prompt tokens in loops (head#38-head#69) |
| | | | | | pass_tainted: | HAR-81 Research-Harbor decision 10:32Z (forbidden network pip install of stevedore at head#33) |
| `har81-p-d-format-code-task-000240__9KYGvwT` | 1.0 (scored) | task_complete_confirmed | none (clean execution) | -- none/n/a | R-NONE-01 | -- |
| `har81-p-d-format-code-task-000434__iT2yUa7` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | head#31 completion/model | R-COMP-02 | head#31 |
| | | | | | note: | error_recovery/model secondary (R-REC-02 wedged terminal): interactive after head#18 (`cd /tmp && rm -rf inittest && mkdir inittest && cd inittest `), 5 turns head#19-head#23 with no interrupt, prompt back after the command finished |
| `har81-p-d-format-code-task-001520__P9EP83h` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | trajectory.cont-1.json#27 completion/model | R-COMP-02 | trajectory.cont-1.json#27,trajectory.cont-1.json#51 |
| | | | | | note: | confirmation_with_call loop (recorded): first confirmation trajectory.cont-1.json#38, span trajectory.cont-1.json#38-trajectory.cont-1.json#51; attribution unclear (confirm vs continue ambiguous) |
| `har81-p-d-format-code-task-001645__bMN27md` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | -- planning/model | R-PLAN-01 | head#35,head#100 |
| | | | | | note: | error_recovery/model secondary: identical run head#35-head#100 |
| `har81-p-d-format-code-task-002537__7xmXPRq` | 0.0 (scored) | ceiling:input_tokens | none (clean execution) | -- planning/model | R-PLAN-01 | head#66,head#78 |
| | | | | | note: | error_recovery/model secondary: identical run head#66-head#78 |

## Learnability: unknown (one attempt per key so far)

Every (task, treatment-key) cluster has a single attempt, so no within-key comparison is possible. No claim is ever made across keys:

| task | treatment key(s) | attempts | scored | passes | status | excluded |
| --- | --- | --- | --- | --- | --- | --- |
| `mimo-v2.6-rl/arvo_18737` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/arvo_41330` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/arvo_42485576` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/arvo_42496599` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/arvo_42514310` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/arvo_42528228` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/candidate-1271-media-games` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/candidate-1559-ml-evaluation` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 1 | unknown | -- |
| `mimo-v2.6-rl/candidate-1634-software-databases` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/candidate-1702-ml-inference` | `sha256:0d4b7a401bf1a0396…` | 0 | 0 | 0 | grader-suspect | har81-p-d-candidate-1702-ml-infe__izs4jgG (R-ENV-02) |
| `mimo-v2.6-rl/candidate-1789-security-appsec` | `sha256:0d4b7a401bf1a0396…` | 0 | 0 | 0 | grader-suspect | har81-p-d-candidate-1789-securit__Nhf2HdJ (R-ENV-02) |
| `mimo-v2.6-rl/candidate-2684-security-appsec` | `sha256:0d4b7a401bf1a0396…` | 0 | 0 | 0 | pass-tainted | har81-p-d-candidate-2684-securit__uwoAzn7 (pass_tainted) |
| `mimo-v2.6-rl/format-code-task-000240` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 1 | unknown | -- |
| `mimo-v2.6-rl/format-code-task-000434` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/format-code-task-001520` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/format-code-task-001645` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |
| `mimo-v2.6-rl/format-code-task-002537` | `sha256:0d4b7a401bf1a0396…` | 1 | 1 | 0 | unknown | -- |

## Loop cost (LOOP-COST)

Per-step prompt tokens over 17 trials: 29,336,364 total; 10,505,516 (35.8%) inside loops (identical runs >=10: 10,054,090; confirmation spans: 451,426; overlapping steps counted once). Proxy input (result.json, includes summarization calls): 29,920,531. 5 step(s) carry no metrics (summarization hand-off turns) and are uncounted, not 0.

| trial | reward | loop prompt tokens | share | spans |
| --- | --- | --- | --- | --- |
| `har81-p-d-arvo-42496599` | 0.0 | 2,233,234 | 93% | identical head#21-head#118 (98) |
| `har81-p-d-format-code-task-001645__bMN27md` | 0.0 | 2,006,226 | 84% | identical head#35-head#100 (66) |
| `har81-p-d-format-code-task-000434__iT2yUa7` | 0.0 | 1,954,370 | 81% | identical head#36-head#104 (69) |
| `har81-p-d-candidate-1271-media-games__grehkae` | 0.0 | 1,739,815 | 73% | identical head#40-head#94 (55) |
| `har81-p-d-candidate-2684-security-appsec__uwoAzn7` | 1.0 | 1,506,688 | 66% | identical head#38-head#69 (32) |
| `har81-p-d-format-code-task-002537__7xmXPRq` | 0.0 | 613,757 | 26% | identical head#66-head#78 (13) |
| `har81-p-d-format-code-task-001520__P9EP83h` | 0.0 | 451,426 | 20% | confirmation trajectory.cont-1.json#38-trajectory.cont-1.json#51 (14) |

## Completion handshake (HANDSHAKE)

8 of 17 trials reached the harness prompt 'Are you sure you want to mark the task as complete? ... include "task_complete": true in your JSON response again.'; 4 confirmed, 4 never did and spent 4,511,793 per-step prompt tokens after the first prompt (15.4% of all per-step prompt tokens). The model writes native tool calls, not JSON; a native turn confirms only when it carries no tool call. 1 trial(s) ran `echo task_complete` as a shell command (har81-p-d-candidate-1702-ml-inference__izs4jgG 58x).

| trial | reward | first prompt | prompts | confirmed | turns after | prompt tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `har81-p-d-format-code-task-000434__iT2yUa7` | 0.0 | head#31 | 1 | no | 73 | 2,036,424 |
| `har81-p-d-candidate-1702-ml-inference__izs4jgG` | 0.0 | head#30 | 1 | no | 66 | 1,951,232 |
| `har81-p-d-format-code-task-001520__P9EP83h` | 0.0 | trajectory.cont-1.json#35 | 7 | no | 16 | 510,926 |
| `har81-p-d-candidate-1634-software-databases__PaSTYBj` | 0.0 | head#39 | 2 | yes | 3 | 67,300 |
| `har81-p-d-arvo-18737` | 0.0 | head#18 | 1 | yes | 1 | 14,869 |
| `har81-p-d-format-code-task-000240__9KYGvwT` | 1.0 | head#20 | 1 | yes | 1 | 13,835 |
| `har81-p-d-candidate-2684-security-appsec__uwoAzn7` | 1.0 | trajectory.cont-1.json#19 | 1 | no | 1 | 13,211 |
| `har81-p-d-candidate-1559-ml-evaluation__bqBv49S` | 1.0 | head#8 | 1 | yes | 1 | 8,017 |

## Wedged terminals (WEDGE, measurement only)

6 stretch(es) in 5 trial(s) of >= 3 executed turns whose keystrokes landed while the shell was not at a prompt (pager, `>` continuation, a program reading stdin or still running). 2 sent an interrupt key; 1 lasted until the run ended. 3,277,988 per-step prompt tokens inside the stretches (11.2% of all). Keystroke source: recorded.

Blind key `validation/har99-wedge.hand-key.jsonl`: trials 16/17 (hand 4 wedged, detector 5); hand stretches matched 4/4; of matched: start exact 3, within 3 4; end exact 3, within 1 4; interrupt 4; prompt back 4; cause 4. Trial disagreements: `har81-p-d-format-code-task-001520`.

| trial | reward | outcome rule | cause | trigger | stretch | turns | interrupt | prompt back | prompt tokens |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `har81-p-d-arvo-42496599` | 0.0 | R-COMP-03 | no_prompt | head#11 | head#12-head#118 | 106 | none | run end | 2,342,322 |
| `har81-p-d-arvo-42485576` | 0.0 | R-COMP-03 | no_prompt | head#47 | head#48-trajectory.cont-1.json#14 | 22 | none | yes | 667,740 |
| `har81-p-d-arvo-42514310` | 0.0 | R-COMP-03 | no_prompt | head#8 | head#9-head#19 | 11 | none | yes | 95,094 |
| `har81-p-d-format-code-task-001520__P9EP83h` | 0.0 | R-COMP-02 | pager | trajectory.cont-1.json#26 | trajectory.cont-1.json#27-trajectory.cont-1.json#29 | 3 | trajectory.cont-1.json#29 (q) | yes | 79,348 |
| `har81-p-d-format-code-task-000434__iT2yUa7` | 0.0 | R-COMP-02 | interactive | head#18 | head#19-head#23 | 5 | none | yes | 75,329 |
| `har81-p-d-format-code-task-001520__P9EP83h` | 0.0 | R-COMP-02 | pager | trajectory.cont-1.json#6 | trajectory.cont-1.json#7-trajectory.cont-1.json#9 | 3 | trajectory.cont-1.json#8 (q) | yes | 18,155 |

## Grader notes (for Data's verifier audit; not R-ENV-02)

- `har81-p-d-arvo-18737` (outcome): deliverable_not_in_instruction: verifier names ['PoC', 'submit', 'submit.sh'] but instruction.md (258 B) never mentions them
- `har81-p-d-candidate-1634-software-databases__PaSTYBj` (outcome): source_text_assertion: failing verifier test asserts literal source strings (test_outputs.py::test_edge_source_modules_compile_and_forward); asserted literal 'def atomic(self, transaction_type=None, **kwargs):' is not in instruction.md; grader audit flag, not R-ENV-02

## Acceptance: recorded vs inferred

Steps with harness-recorded step_layers use the recorded verdict; the observation-inferred (H-ACC) value is kept only as a cross-check:

| trial | recorded steps | agree accept | agree reject | disagree | inferred unknown |
| --- | --- | --- | --- | --- | --- |
| `har81-p-d-arvo-18737` | 18 | 16 | 2 | 0 | 0 |
| `har81-p-d-arvo-41330` | 101 | 101 | 0 | 0 | 0 |
| `har81-p-d-arvo-42485576` | 94 | 94 | 0 | 0 | 0 |
| `har81-p-d-arvo-42496599` | 117 | 116 | 1 | 0 | 0 |
| `har81-p-d-arvo-42514310` | 80 | 80 | 0 | 0 | 0 |
| `har81-p-d-arvo-42528228` | 79 | 77 | 2 | 0 | 0 |
| `har81-p-d-candidate-1271-media-games__grehkae` | 93 | 93 | 0 | 0 | 0 |
| `har81-p-d-candidate-1559-ml-evaluation__bqBv49S` | 8 | 7 | 1 | 0 | 0 |
| `har81-p-d-candidate-1634-software-databases__PaSTYBj` | 41 | 41 | 0 | 0 | 0 |
| `har81-p-d-candidate-1702-ml-inference__izs4jgG` | 95 | 95 | 0 | 0 | 0 |
| `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ` | 23 | 23 | 0 | 0 | 0 |
| `har81-p-d-candidate-2684-security-appsec__uwoAzn7` | 84 | 41 | 43 | 0 | 0 |
| `har81-p-d-format-code-task-000240__9KYGvwT` | 20 | 20 | 0 | 0 | 0 |
| `har81-p-d-format-code-task-000434__iT2yUa7` | 103 | 103 | 0 | 0 | 0 |
| `har81-p-d-format-code-task-001520__P9EP83h` | 90 | 90 | 0 | 0 | 0 |
| `har81-p-d-format-code-task-001645__bMN27md` | 99 | 99 | 0 | 0 | 0 |
| `har81-p-d-format-code-task-002537__7xmXPRq` | 77 | 77 | 0 | 0 | 0 |

## Capture coverage

Episodes by count AND token-attributed share are both shown: a step count matching n_episodes is NOT full coverage when tokens are unattributed (har81-p-d-arvo-41330: conts -- missing, 21557 unattributed -> 0.9911 share; har81-p-d-arvo-42485576: conts -- missing, 29590 unattributed -> 0.9878 share; har81-p-d-arvo-42528228: conts -- missing, 22799 unattributed -> 0.9907 share; har81-p-d-candidate-1789-security-appsec__Nhf2HdJ: conts -- missing, 17875 unattributed -> 0.9363 share; har81-p-d-candidate-2684-security-appsec__uwoAzn7: conts -- missing, 70898 unattributed -> 0.9712 share).

| trial | assembly | head | cont files | summ files | steps vs episodes | conts missing | token share (traj/proxy) | unattributed input | standins | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `har81-p-d-arvo-18737` | single_head | True | -- | 0 | 18/18 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-arvo-41330` | new_session | True | [1] | 3 | 102/102 | -- | 0.9911 | 21557 | -- | -- |
| `har81-p-d-arvo-42485576` | new_session | True | [1] | 3 | 95/95 | -- | 0.9878 | 29590 | -- | -- |
| `har81-p-d-arvo-42496599` | single_head | True | -- | 0 | 117/118 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-arvo-42514310` | single_head | True | -- | 0 | 80/81 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-arvo-42528228` | new_session | True | [1] | 3 | 80/80 | -- | 0.9907 | 22799 | -- | -- |
| `har81-p-d-candidate-1271-media-games__grehkae` | single_head | True | -- | 0 | 93/94 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-candidate-1559-ml-evaluation__bqBv49S` | single_head | True | -- | 0 | 8/8 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-candidate-1634-software-databases__PaSTYBj` | single_head | True | -- | 0 | 41/41 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-candidate-1702-ml-inference__izs4jgG` | single_head | True | -- | 0 | 95/96 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ` | single_head | True | -- | 0 | 23/24 | -- | 0.9363 | 17875 | -- | -- |
| `har81-p-d-candidate-2684-security-appsec__uwoAzn7` | new_session | True | [1] | 3 | 85/85 | -- | 0.9712 | 70898 | -- | -- |
| `har81-p-d-format-code-task-000240__9KYGvwT` | single_head | True | -- | 0 | 20/20 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-format-code-task-000434__iT2yUa7` | single_head | True | -- | 0 | 103/104 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-format-code-task-001520__P9EP83h` | new_session | True | [1] | 3 | 91/91 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-format-code-task-001645__bMN27md` | single_head | True | -- | 0 | 99/100 | -- | 1.0 | 0 | -- | -- |
| `har81-p-d-format-code-task-002537__7xmXPRq` | single_head | True | -- | 0 | 77/78 | -- | 1.0 | 0 | -- | -- |

## Provenance

- evallab-src: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/src`
- evallab commit: `7de1ce6e4b9da11f2af0867cb2314896623e412e`
- normalizer sha256 (`mimo_tool_calls.py`): `eb61671b612274aeff06eaa115a856cacd27464e47e1614b4635d35f9ab6ba54`
- normalizer mode: `evallab-normalizer`
- HAR-93 treatment parquet sha256: `9759017ffb8a4946aabf15ea3056316f788e479e1912ea58da463556f0ec9487`
- HAR-93 capture parquet sha256: `14f24c1b6469f3d38afc73d74dfe2c8047ce08deddc84dc26b655691c1db7b4a`
- nop runs dir: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/mimo-ops/runs`

## Limits

- Replaying old messages through today's normalizer recovers what the model PROPOSED; it is never what the harness accepted/executed then. Acceptance comes only from that step's recorded observation, or from recorded step_layers where the harness wrote them.
- Anything absent is null/`unknown`, never 0.
- Treatment keys: Infra's pinned dispatch key (`<worktree>/derived/<lane>/treatment-key.json`) first, then Data's HAR-93 trial_treatment.parquet setup_key (sha256 recorded above; attached as a cross-check wherever it exists), then a lab-metadata pin; config-derived keys are only a fallback.
- A (task, key) cluster is `learnable` only with 2+ scored attempts and mixed outcomes; `all-pass`/`all-fail` need 2+ scored attempts; otherwise `unknown`. No claim is ever made across keys except a declared, recorded equivalence.

## Validation vs hand key

- tag: 17/17 agree
- attribution: 17/17 agree
- stop: 17/17 agree
- rule: 17/17 agree

No disagreements.

