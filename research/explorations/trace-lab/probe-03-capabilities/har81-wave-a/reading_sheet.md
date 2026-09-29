# Reading sheet (hand labeling, HAR-91 proposals)

_Source: har81-wave-a/capabilities.jsonl; 17 traces balanced across outcome tag x attribution (round-robin, distinct tasks first). Open each trace, read it end to end, then fill one table row and CONFIRM or CORRECT the proposed tag (the last three columns are proposals, not labels)._

## Traces to read

### completion: `har81-p-d-arvo-41330__Zi79ug4`

- task: `mimo-v2.6-rl/arvo_41330`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 102, calls reconstructed: 123, tokens: {'input': 2403866, 'output': 23387}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-03, step head#32)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-41330/har81-p-d-arvo-41330__Zi79ug4`

### completion: `har81-p-d-arvo-18737__8bHpbg3`

- task: `mimo-v2.6-rl/arvo_18737`
- reward: 0.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 18, calls reconstructed: 21, tokens: {'input': 181523, 'output': 3160}
- first failure: tool_use/model (R-TOOL-02, step head#16, recovered=true)
- outcome-relevant: completion/unclear (R-COMP-03, step head#18)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-18737/har81-p-d-arvo-18737__8bHpbg3`

### environment: `har81-p-d-candidate-1702-ml-inference__izs4jgG`

- task: `mimo-v2.6-rl/candidate-1702-ml-inference`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 95, calls reconstructed: 97, tokens: {'input': 2386480, 'output': 14067}
- first failure: environment/harness (R-ENV-02, step head#2, recovered=false)
- outcome-relevant: environment/harness (R-ENV-02, step head#2)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1702-ml-inference/har81-p-d-candidate-1702-ml-infe__izs4jgG`

### none: `har81-p-d-candidate-1559-ml-evaluation__bqBv49S`

- task: `mimo-v2.6-rl/candidate-1559-ml-evaluation`
- reward: 1.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 8, calls reconstructed: 7, tokens: {'input': 46630, 'output': 1535}
- first failure: completion/harness (R-COMP-01, step head#7, recovered=true)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1559-ml-evaluation/har81-p-d-candidate-1559-ml-eval__bqBv49S`

### planning: `har81-p-d-candidate-1271-media-games__grehkae`

- task: `mimo-v2.6-rl/candidate-1271-media-games`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 93, calls reconstructed: 104, tokens: {'input': 2379211, 'output': 6415}
- first failure: none (clean execution)
- outcome-relevant: planning/model (R-PLAN-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1271-media-games/har81-p-d-candidate-1271-media-g__grehkae`

### completion: `har81-p-d-arvo-42485576__dsgmV5c`

- task: `mimo-v2.6-rl/arvo_42485576`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 95, calls reconstructed: 101, tokens: {'input': 2394644, 'output': 21516}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-03, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42485576/har81-p-d-arvo-42485576__dsgmV5c`

### environment: `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ`

- task: `mimo-v2.6-rl/candidate-1789-security-appsec`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 23, calls reconstructed: 29, tokens: {'input': 280515, 'output': 4390}
- first failure: environment/harness (R-ENV-02, step head#7, recovered=false)
- outcome-relevant: environment/harness (R-ENV-02, step head#7)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1789-security-appsec/har81-p-d-candidate-1789-securit__Nhf2HdJ`

### none: `har81-p-d-candidate-2684-security-appsec__uwoAzn7`

- task: `mimo-v2.6-rl/candidate-2684-security-appsec`
- reward: 1.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 85, calls reconstructed: 71, tokens: {'input': 2390297, 'output': 18739}
- first failure: tool_use/model (R-TOOL-02, step head#2, recovered=true)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-2684-security-appsec/har81-p-d-candidate-2684-securit__uwoAzn7`

### planning: `har81-p-d-format-code-task-001645__bMN27md`

- task: `mimo-v2.6-rl/format-code-task-001645`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 99, calls reconstructed: 101, tokens: {'input': 2379637, 'output': 10842}
- first failure: none (clean execution)
- outcome-relevant: planning/model (R-PLAN-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-001645/har81-p-d-format-code-task-00164__bMN27md`

### completion: `har81-p-d-arvo-42496599__GkwMiLe`

- task: `mimo-v2.6-rl/arvo_42496599`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 117, calls reconstructed: 124, tokens: {'input': 2398339, 'output': 12919}
- first failure: tool_use/model (R-TOOL-02, step head#14, recovered=true)
- outcome-relevant: completion/model (R-COMP-03, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42496599/har81-p-d-arvo-42496599__GkwMiLe`

### none: `har81-p-d-format-code-task-000240__9KYGvwT`

- task: `mimo-v2.6-rl/format-code-task-000240`
- reward: 1.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 20, calls reconstructed: 23, tokens: {'input': 185136, 'output': 4563}
- first failure: none (clean execution)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000240/har81-p-d-format-code-task-00024__9KYGvwT`

### planning: `har81-p-d-format-code-task-002537__7xmXPRq`

- task: `mimo-v2.6-rl/format-code-task-002537`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 77, calls reconstructed: 80, tokens: {'input': 2374328, 'output': 7342}
- first failure: none (clean execution)
- outcome-relevant: planning/model (R-PLAN-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-002537/har81-p-d-format-code-task-00253__7xmXPRq`

### completion: `har81-p-d-arvo-42514310__BGTEMs2`

- task: `mimo-v2.6-rl/arvo_42514310`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 80, calls reconstructed: 84, tokens: {'input': 2355630, 'output': 6834}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-03, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42514310/har81-p-d-arvo-42514310__BGTEMs2`

### completion: `har81-p-d-arvo-42528228__8WpUvat`

- task: `mimo-v2.6-rl/arvo_42528228`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 80, calls reconstructed: 103, tokens: {'input': 2428062, 'output': 12139}
- first failure: tool_use/model (R-TOOL-02, step head#4, recovered=true)
- outcome-relevant: completion/model (R-COMP-03, step head#15)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42528228/har81-p-d-arvo-42528228__8WpUvat`

### completion: `har81-p-d-candidate-1634-software-databases__PaSTYBj`

- task: `mimo-v2.6-rl/candidate-1634-software-databases`
- reward: 0.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 41, calls reconstructed: 40, tokens: {'input': 534632, 'output': 5848}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-02, step head#39)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1634-software-databases/har81-p-d-candidate-1634-softwar__PaSTYBj`

### completion: `har81-p-d-format-code-task-000434__iT2yUa7`

- task: `mimo-v2.6-rl/format-code-task-000434`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 103, calls reconstructed: 105, tokens: {'input': 2406537, 'output': 12428}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-02, step head#31)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000434/har81-p-d-format-code-task-00043__iT2yUa7`

### completion: `har81-p-d-format-code-task-001520__P9EP83h`

- task: `mimo-v2.6-rl/format-code-task-001520`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 91, calls reconstructed: 83, tokens: {'input': 2395064, 'output': 31590}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-02, step trajectory.cont-1.json#27)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-001520/har81-p-d-format-code-task-00152__P9EP83h`

## Labeling table

| trial | what the agent tried | where it went wrong | failure label (free text) | is the task at fault (y/n/unsure) | evidence line | proposed tag | attribution | evidence steps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `har81-p-d-arvo-41330__Zi79ug4` |  |  |  |  | outcome=completion/model (R-COMP-03); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=102 acc={'true': 101, 'false': 0, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-41330/har81-p-d-arvo-41330__Zi79ug4; first none (clean execution); outcome completion/model @ head#30,head#32 (R-COMP-03) | completion | model | head#30,head#32 |
| `har81-p-d-arvo-18737__8bHpbg3` |  |  |  |  | outcome=completion/unclear (R-COMP-03); first=tool_use/model (R-TOOL-02, recovered=true); stop=task_complete_confirmed; reward=0.0 (scored=True); turns=18 acc={'true': 16, 'false': 2, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-18737/har81-p-d-arvo-18737__8bHpbg3; first tool_use/model @ head#16 (R-TOOL-02); outcome completion/unclear @ head#18,head#19 (R-COMP-03) | completion | unclear | head#18,head#19 |
| `har81-p-d-candidate-1702-ml-inference__izs4jgG` |  |  |  |  | outcome=environment/harness (R-ENV-02); first=environment/harness (R-ENV-02, recovered=false); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=95 acc={'true': 95, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1702-ml-inference/har81-p-d-candidate-1702-ml-infe__izs4jgG; first environment/harness @ head#2 (R-ENV-02); outcome environment/harness @ head#2 (R-ENV-02) | environment | harness | head#2 |
| `har81-p-d-candidate-1559-ml-evaluation__bqBv49S` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=completion/harness (R-COMP-01, recovered=true); stop=task_complete_confirmed; reward=1.0 (scored=True); turns=8 acc={'true': 7, 'false': 1, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1559-ml-evaluation/har81-p-d-candidate-1559-ml-eval__bqBv49S; first completion/harness @ head#7 (R-COMP-01); outcome none/n/a @ -- (R-NONE-01) | none | n/a | -- |
| `har81-p-d-candidate-1271-media-games__grehkae` |  |  |  |  | outcome=planning/model (R-PLAN-01); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=93 acc={'true': 93, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1271-media-games/har81-p-d-candidate-1271-media-g__grehkae; first none (clean execution); outcome planning/model @ head#40,head#94 (R-PLAN-01) | planning | model | head#40,head#94 |
| `har81-p-d-arvo-42485576__dsgmV5c` |  |  |  |  | outcome=completion/model (R-COMP-03); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=95 acc={'true': 94, 'false': 0, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42485576/har81-p-d-arvo-42485576__dsgmV5c; first none (clean execution); outcome completion/model @ head#34 (R-COMP-03) | completion | model | head#34 |
| `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ` |  |  |  |  | outcome=environment/harness (R-ENV-02); first=environment/harness (R-ENV-02, recovered=false); stop=agent_timeout; reward=0.0 (scored=True); turns=23 acc={'true': 23, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1789-security-appsec/har81-p-d-candidate-1789-securit__Nhf2HdJ; first environment/harness @ head#7 (R-ENV-02); outcome environment/harness @ head#7 (R-ENV-02) | environment | harness | head#7 |
| `har81-p-d-candidate-2684-security-appsec__uwoAzn7` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=tool_use/model (R-TOOL-02, recovered=true); stop=ceiling:input_tokens; reward=1.0 (scored=True); turns=85 acc={'true': 41, 'false': 43, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-2684-security-appsec/har81-p-d-candidate-2684-securit__uwoAzn7; first tool_use/model @ head#2 (R-TOOL-02); outcome none/n/a @ head#38,head#69 (R-NONE-01) | none | n/a | head#38,head#69 |
| `har81-p-d-format-code-task-001645__bMN27md` |  |  |  |  | outcome=planning/model (R-PLAN-01); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=99 acc={'true': 99, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-001645/har81-p-d-format-code-task-00164__bMN27md; first none (clean execution); outcome planning/model @ head#35,head#100 (R-PLAN-01) | planning | model | head#35,head#100 |
| `har81-p-d-arvo-42496599__GkwMiLe` |  |  |  |  | outcome=completion/model (R-COMP-03); first=tool_use/model (R-TOOL-02, recovered=true); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=117 acc={'true': 116, 'false': 1, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42496599/har81-p-d-arvo-42496599__GkwMiLe; first tool_use/model @ head#14 (R-TOOL-02); outcome completion/model @ head#3,head#21,head#118 (R-COMP-03) | completion | model | head#3,head#21,head#118 |
| `har81-p-d-format-code-task-000240__9KYGvwT` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=none (clean execution); stop=task_complete_confirmed; reward=1.0 (scored=True); turns=20 acc={'true': 20, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000240/har81-p-d-format-code-task-00024__9KYGvwT; first none (clean execution); outcome none/n/a @ -- (R-NONE-01) | none | n/a | -- |
| `har81-p-d-format-code-task-002537__7xmXPRq` |  |  |  |  | outcome=planning/model (R-PLAN-01); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=77 acc={'true': 77, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-002537/har81-p-d-format-code-task-00253__7xmXPRq; first none (clean execution); outcome planning/model @ head#66,head#78 (R-PLAN-01) | planning | model | head#66,head#78 |
| `har81-p-d-arvo-42514310__BGTEMs2` |  |  |  |  | outcome=completion/model (R-COMP-03); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=80 acc={'true': 80, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42514310/har81-p-d-arvo-42514310__BGTEMs2; first none (clean execution); outcome completion/model @ head#3,head#6 (R-COMP-03) | completion | model | head#3,head#6 |
| `har81-p-d-arvo-42528228__8WpUvat` |  |  |  |  | outcome=completion/model (R-COMP-03); first=tool_use/model (R-TOOL-02, recovered=true); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=80 acc={'true': 77, 'false': 2, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42528228/har81-p-d-arvo-42528228__8WpUvat; first tool_use/model @ head#4 (R-TOOL-02); outcome completion/model @ trajectory.cont-1.json#6,head#14,head#15 (R-COMP-03) | completion | model | trajectory.cont-1.json#6,head#14,head#15 |
| `har81-p-d-candidate-1634-software-databases__PaSTYBj` |  |  |  |  | outcome=completion/model (R-COMP-02); first=none (clean execution); stop=task_complete_confirmed; reward=0.0 (scored=True); turns=41 acc={'true': 41, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1634-software-databases/har81-p-d-candidate-1634-softwar__PaSTYBj; first none (clean execution); outcome completion/model @ head#39,head#41,head#42 (R-COMP-02) | completion | model | head#39,head#41,head#42 |
| `har81-p-d-format-code-task-000434__iT2yUa7` |  |  |  |  | outcome=completion/model (R-COMP-02); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=103 acc={'true': 103, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000434/har81-p-d-format-code-task-00043__iT2yUa7; first none (clean execution); outcome completion/model @ head#31 (R-COMP-02) | completion | model | head#31 |
| `har81-p-d-format-code-task-001520__P9EP83h` |  |  |  |  | outcome=completion/model (R-COMP-02); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=91 acc={'true': 90, 'false': 0, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-001520/har81-p-d-format-code-task-00152__P9EP83h; first none (clean execution); outcome completion/model @ trajectory.cont-1.json#27,trajectory.cont-1.json#51 (R-COMP-02) | completion | model | trajectory.cont-1.json#27,trajectory.cont-1.json#51 |

## After labeling

- Where your hand tag differs from the proposal, cite the step that changed your mind.
- If 2+ rows blame the task (not the agent), flag the task for the env audit; if rows show the same agent loop twice, note the loop shape for the harness fix.
