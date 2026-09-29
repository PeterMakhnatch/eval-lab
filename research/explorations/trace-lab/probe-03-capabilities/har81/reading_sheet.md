# Reading sheet (hand labeling, HAR-91 proposals)

_Source: har81/capabilities.jsonl; 21 hand-picked traces (--sheet-picks). Open each trace, read it end to end, then fill one table row and CONFIRM or CORRECT the proposed tag (the last three columns are proposals, not labels)._

## Traces to read

### none: `har81-p-d-format-code-task-000240__9KYGvwT`

- task: `mimo-v2.6-rl/format-code-task-000240`
- reward: 1.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 20, calls reconstructed: 23, tokens: {'input': 185136, 'output': 4563}
- first failure: none (clean execution)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000240/har81-p-d-format-code-task-00024__9KYGvwT`

### none: `har81-l-d-a2-format-code-task-001520__6TqzNQw`

- task: `mimo-v2.6-rl/format-code-task-001520`
- reward: 1.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 95, calls reconstructed: 94, tokens: {'input': 2372667, 'output': 22561}
- first failure: none (clean execution)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a2-format-code-task-001520/har81-l-d-a2-format-code-task-00__6TqzNQw`

### none: `har81-l-d-a3-arvo-18737__FygpNSe`

- task: `mimo-v2.6-rl/arvo_18737`
- reward: 1.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 79, calls reconstructed: 76, tokens: {'input': 2372255, 'output': 9661}
- first failure: tool_use/model (R-TOOL-02, step head#4, recovered=true)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a3-arvo-18737/har81-l-d-a3-arvo-18737__FygpNSe`

### none: `har81-p-d-candidate-2684-security-appsec__uwoAzn7`

- task: `mimo-v2.6-rl/candidate-2684-security-appsec`
- reward: 1.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 85, calls reconstructed: 71, tokens: {'input': 2390297, 'output': 18739}
- first failure: tool_use/model (R-TOOL-02, step head#2, recovered=true)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-2684-security-appsec/har81-p-d-candidate-2684-securit__uwoAzn7`

### none: `har81-l-d-a2-arvo-42485576__YQ3rQ7R`

- task: `mimo-v2.6-rl/arvo_42485576`
- reward: 1.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 35, calls reconstructed: 50, tokens: {'input': 895817, 'output': 10851}
- first failure: tool_use/unclear (R-TOOL-01U, step head#2, recovered=true)
- outcome-relevant: none/n/a (R-NONE-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a2-arvo-42485576/har81-l-d-a2-arvo-42485576__YQ3rQ7R`

### completion: `har81-p-d-candidate-1634-software-databases__PaSTYBj`

- task: `mimo-v2.6-rl/candidate-1634-software-databases`
- reward: 0.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 41, calls reconstructed: 40, tokens: {'input': 534632, 'output': 5848}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-02, step head#39)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1634-software-databases/har81-p-d-candidate-1634-softwar__PaSTYBj`

### completion: `har81-l-d-a4-candidate-1634-software-databases__TJN8GB5`

- task: `mimo-v2.6-rl/candidate-1634-software-databases`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 47, calls reconstructed: 49, tokens: {'input': 1003696, 'output': 9405}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-02, step head#21)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a4-candidate-1634-software-databases/har81-l-d-a4-candidate-1634-soft__TJN8GB5`

### completion: `har81-l-d-a2-format-code-task-000240__NGhDDRU`

- task: `mimo-v2.6-rl/format-code-task-000240`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 89, calls reconstructed: 94, tokens: {'input': 2346610, 'output': 16150}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-02, step head#16)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a2-format-code-task-000240/har81-l-d-a2-format-code-task-00__NGhDDRU`

### completion: `har81-p-d-format-code-task-000434__iT2yUa7`

- task: `mimo-v2.6-rl/format-code-task-000434`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 103, calls reconstructed: 105, tokens: {'input': 2406537, 'output': 12428}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-02, step head#31)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000434/har81-p-d-format-code-task-00043__iT2yUa7`

### completion: `har81-p-d-arvo-42528228__8WpUvat`

- task: `mimo-v2.6-rl/arvo_42528228`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 80, calls reconstructed: 103, tokens: {'input': 2428062, 'output': 12139}
- first failure: tool_use/model (R-TOOL-02, step head#4, recovered=true)
- outcome-relevant: completion/model (R-COMP-03, step head#15)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42528228/har81-p-d-arvo-42528228__8WpUvat`

### completion: `har81-l-d-a3-arvo-42485576__MNqUNYv`

- task: `mimo-v2.6-rl/arvo_42485576`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 79, calls reconstructed: 86, tokens: {'input': 2340519, 'output': 4259}
- first failure: none (clean execution)
- outcome-relevant: completion/model (R-COMP-03, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a3-arvo-42485576/har81-l-d-a3-arvo-42485576__MNqUNYv`

### completion: `har81-p-d-arvo-57589__xvqMDKd`

- task: `mimo-v2.6-rl/arvo_57589`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 91, calls reconstructed: 100, tokens: {'input': 2407562, 'output': 9426}
- first failure: none (clean execution)
- outcome-relevant: completion/unclear (R-COMP-03, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-p-d-arvo-57589/har81-p-d-arvo-57589__xvqMDKd`

### completion: `har81-p-d-arvo-18737__8bHpbg3`

- task: `mimo-v2.6-rl/arvo_18737`
- reward: 0.0 (scored=True)
- stop: task_complete_confirmed (None)
- turns read: 18, calls reconstructed: 21, tokens: {'input': 181523, 'output': 3160}
- first failure: tool_use/model (R-TOOL-02, step head#16, recovered=true)
- outcome-relevant: completion/unclear (R-COMP-03, step head#18)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-18737/har81-p-d-arvo-18737__8bHpbg3`

### completion: `har81-p-d-arvo-42496599__GkwMiLe`

- task: `mimo-v2.6-rl/arvo_42496599`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 117, calls reconstructed: 124, tokens: {'input': 2398339, 'output': 12919}
- first failure: tool_use/model (R-TOOL-02, step head#14, recovered=true)
- outcome-relevant: completion/model (R-COMP-03, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42496599/har81-p-d-arvo-42496599__GkwMiLe`

### planning: `har81-p-d-candidate-1271-media-games__grehkae`

- task: `mimo-v2.6-rl/candidate-1271-media-games`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 93, calls reconstructed: 104, tokens: {'input': 2379211, 'output': 6415}
- first failure: none (clean execution)
- outcome-relevant: planning/model (R-PLAN-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1271-media-games/har81-p-d-candidate-1271-media-g__grehkae`

### planning: `har81-p-d-format-code-task-003011__4KvLSba`

- task: `mimo-v2.6-rl/format-code-task-003011`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 73, calls reconstructed: 97, tokens: {'input': 2334662, 'output': 8287}
- first failure: none (clean execution)
- outcome-relevant: planning/model (R-PLAN-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-p-d-format-code-task-003011/har81-p-d-format-code-task-00301__4KvLSba`

### planning: `har81-p-d-candidate-1048-operations-virtualization__Rh9y42B`

- task: `mimo-v2.6-rl/candidate-1048-operations-virtualization`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 90, calls reconstructed: 101, tokens: {'input': 2391642, 'output': 6040}
- first failure: tool_use/unclear (R-TOOL-01U, step head#39, recovered=true)
- outcome-relevant: planning/model (R-PLAN-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-p-d-candidate-1048-operations-virtualization/har81-p-d-candidate-1048-operati__Rh9y42B`

### planning: `har81-l-d-a4-format-code-task-000240__2JNXcHz`

- task: `mimo-v2.6-rl/format-code-task-000240`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 72, calls reconstructed: 73, tokens: {'input': 238907, 'output': 2690}
- first failure: none (clean execution)
- outcome-relevant: planning/model (R-PLAN-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a4-format-code-task-000240/har81-l-d-a4-format-code-task-00__2JNXcHz`

### environment: `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ`

- task: `mimo-v2.6-rl/candidate-1789-security-appsec`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 23, calls reconstructed: 29, tokens: {'input': 280515, 'output': 4390}
- first failure: environment/harness (R-ENV-02, step head#7, recovered=false)
- outcome-relevant: environment/harness (R-ENV-02, step head#7)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1789-security-appsec/har81-p-d-candidate-1789-securit__Nhf2HdJ`

### environment: `har81-p-d-candidate-1702-ml-inference__izs4jgG`

- task: `mimo-v2.6-rl/candidate-1702-ml-inference`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 95, calls reconstructed: 97, tokens: {'input': 2386480, 'output': 14067}
- first failure: environment/harness (R-ENV-02, step head#2, recovered=false)
- outcome-relevant: environment/harness (R-ENV-02, step head#2)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1702-ml-inference/har81-p-d-candidate-1702-ml-infe__izs4jgG`

### environment: `har81-l-d-a3-candidate-1789-security-appsec__WacRiXN`

- task: `mimo-v2.6-rl/candidate-1789-security-appsec`
- reward: 0.0 (scored=True)
- stop: ceiling:input_tokens (TrialBudgetExhaustedError)
- turns read: 115, calls reconstructed: 117, tokens: {'input': 2419203, 'output': 8687}
- first failure: environment/harness (R-ENV-02, step head#6, recovered=false)
- outcome-relevant: environment/harness (R-ENV-02, step head#6)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a3-candidate-1789-security-appsec/har81-l-d-a3-candidate-1789-secu__WacRiXN`

## Labeling table

| trial | what the agent tried | where it went wrong | failure label (free text) | is the task at fault (y/n/unsure) | evidence line | proposed tag | attribution | evidence steps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `har81-p-d-format-code-task-000240__9KYGvwT` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=none (clean execution); stop=task_complete_confirmed; reward=1.0 (scored=True); turns=20 acc={'true': 20, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000240/har81-p-d-format-code-task-00024__9KYGvwT; first none (clean execution); outcome none/n/a @ -- (R-NONE-01) | none | n/a | -- |
| `har81-l-d-a2-format-code-task-001520__6TqzNQw` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=none (clean execution); stop=ceiling:input_tokens; reward=1.0 (scored=True); turns=95 acc={'true': 95, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a2-format-code-task-001520/har81-l-d-a2-format-code-task-00__6TqzNQw; first none (clean execution); outcome none/n/a @ head#22,head#96 (R-NONE-01) | none | n/a | head#22,head#96 |
| `har81-l-d-a3-arvo-18737__FygpNSe` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=tool_use/model (R-TOOL-02, recovered=true); stop=ceiling:input_tokens; reward=1.0 (scored=True); turns=79 acc={'true': 76, 'false': 3, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a3-arvo-18737/har81-l-d-a3-arvo-18737__FygpNSe; first tool_use/model @ head#4 (R-TOOL-02); outcome none/n/a @ head#71,head#80 (R-NONE-01) | none | n/a | head#71,head#80 |
| `har81-p-d-candidate-2684-security-appsec__uwoAzn7` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=tool_use/model (R-TOOL-02, recovered=true); stop=ceiling:input_tokens; reward=1.0 (scored=True); turns=85 acc={'true': 41, 'false': 43, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-2684-security-appsec/har81-p-d-candidate-2684-securit__uwoAzn7; first tool_use/model @ head#2 (R-TOOL-02); outcome none/n/a @ head#38,head#69 (R-NONE-01) | none | n/a | head#38,head#69 |
| `har81-l-d-a2-arvo-42485576__YQ3rQ7R` |  |  |  |  | outcome=none/n/a (R-NONE-01); first=tool_use/unclear (R-TOOL-01U, recovered=true); stop=task_complete_confirmed; reward=1.0 (scored=True); turns=35 acc={'true': 31, 'false': 4, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a2-arvo-42485576/har81-l-d-a2-arvo-42485576__YQ3rQ7R; first tool_use/unclear @ head#2 (R-TOOL-01U); outcome none/n/a @ -- (R-NONE-01) | none | n/a | -- |
| `har81-p-d-candidate-1634-software-databases__PaSTYBj` |  |  |  |  | outcome=completion/model (R-COMP-02); first=none (clean execution); stop=task_complete_confirmed; reward=0.0 (scored=True); turns=41 acc={'true': 41, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1634-software-databases/har81-p-d-candidate-1634-softwar__PaSTYBj; first none (clean execution); outcome completion/model @ head#39,head#41,head#42 (R-COMP-02) | completion | model | head#39,head#41,head#42 |
| `har81-l-d-a4-candidate-1634-software-databases__TJN8GB5` |  |  |  |  | outcome=completion/model (R-COMP-02); first=none (clean execution); stop=agent_timeout; reward=0.0 (scored=True); turns=47 acc={'true': 47, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a4-candidate-1634-software-databases/har81-l-d-a4-candidate-1634-soft__TJN8GB5; first none (clean execution); outcome completion/model @ head#21 (R-COMP-02) | completion | model | head#21 |
| `har81-l-d-a2-format-code-task-000240__NGhDDRU` |  |  |  |  | outcome=completion/model (R-COMP-02); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=89 acc={'true': 89, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a2-format-code-task-000240/har81-l-d-a2-format-code-task-00__NGhDDRU; first none (clean execution); outcome completion/model @ head#16,head#22,head#90 (R-COMP-02) | completion | model | head#16,head#22,head#90 |
| `har81-p-d-format-code-task-000434__iT2yUa7` |  |  |  |  | outcome=completion/model (R-COMP-02); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=103 acc={'true': 103, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-format-code-task-000434/har81-p-d-format-code-task-00043__iT2yUa7; first none (clean execution); outcome completion/model @ head#31 (R-COMP-02) | completion | model | head#31 |
| `har81-p-d-arvo-42528228__8WpUvat` |  |  |  |  | outcome=completion/model (R-COMP-03); first=tool_use/model (R-TOOL-02, recovered=true); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=80 acc={'true': 77, 'false': 2, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42528228/har81-p-d-arvo-42528228__8WpUvat; first tool_use/model @ head#4 (R-TOOL-02); outcome completion/model @ trajectory.cont-1.json#6,head#14,head#15 (R-COMP-03) | completion | model | trajectory.cont-1.json#6,head#14,head#15 |
| `har81-l-d-a3-arvo-42485576__MNqUNYv` |  |  |  |  | outcome=completion/model (R-COMP-03); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=79 acc={'true': 79, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a3-arvo-42485576/har81-l-d-a3-arvo-42485576__MNqUNYv; first none (clean execution); outcome completion/model @ head#4,head#7,head#80 (R-COMP-03) | completion | model | head#4,head#7,head#80 |
| `har81-p-d-arvo-57589__xvqMDKd` |  |  |  |  | outcome=completion/unclear (R-COMP-03); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=91 acc={'true': 91, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-p-d-arvo-57589/har81-p-d-arvo-57589__xvqMDKd; first none (clean execution); outcome completion/unclear @ head#34,head#92 (R-COMP-03) | completion | unclear | head#34,head#92 |
| `har81-p-d-arvo-18737__8bHpbg3` |  |  |  |  | outcome=completion/unclear (R-COMP-03); first=tool_use/model (R-TOOL-02, recovered=true); stop=task_complete_confirmed; reward=0.0 (scored=True); turns=18 acc={'true': 16, 'false': 2, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-18737/har81-p-d-arvo-18737__8bHpbg3; first tool_use/model @ head#16 (R-TOOL-02); outcome completion/unclear @ head#18,head#19 (R-COMP-03) | completion | unclear | head#18,head#19 |
| `har81-p-d-arvo-42496599__GkwMiLe` |  |  |  |  | outcome=completion/model (R-COMP-03); first=tool_use/model (R-TOOL-02, recovered=true); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=117 acc={'true': 116, 'false': 1, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-arvo-42496599/har81-p-d-arvo-42496599__GkwMiLe; first tool_use/model @ head#14 (R-TOOL-02); outcome completion/model @ head#3,head#21,head#118 (R-COMP-03) | completion | model | head#3,head#21,head#118 |
| `har81-p-d-candidate-1271-media-games__grehkae` |  |  |  |  | outcome=planning/model (R-PLAN-01); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=93 acc={'true': 93, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1271-media-games/har81-p-d-candidate-1271-media-g__grehkae; first none (clean execution); outcome planning/model @ head#40,head#94 (R-PLAN-01) | planning | model | head#40,head#94 |
| `har81-p-d-format-code-task-003011__4KvLSba` |  |  |  |  | outcome=planning/model (R-PLAN-01); first=none (clean execution); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=73 acc={'true': 73, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-p-d-format-code-task-003011/har81-p-d-format-code-task-00301__4KvLSba; first none (clean execution); outcome planning/model @ head#2,head#74 (R-PLAN-01) | planning | model | head#2,head#74 |
| `har81-p-d-candidate-1048-operations-virtualization__Rh9y42B` |  |  |  |  | outcome=planning/model (R-PLAN-01); first=tool_use/unclear (R-TOOL-01U, recovered=true); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=90 acc={'true': 89, 'false': 1, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-p-d-candidate-1048-operations-virtualization/har81-p-d-candidate-1048-operati__Rh9y42B; first tool_use/unclear @ head#39 (R-TOOL-01U); outcome planning/model @ head#49,head#91 (R-PLAN-01) | planning | model | head#49,head#91 |
| `har81-l-d-a4-format-code-task-000240__2JNXcHz` |  |  |  |  | outcome=planning/model (R-PLAN-01); first=none (clean execution); stop=agent_timeout; reward=0.0 (scored=True); turns=72 acc={'true': 72, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a4-format-code-task-000240/har81-l-d-a4-format-code-task-00__2JNXcHz; first none (clean execution); outcome planning/model @ head#18,head#73 (R-PLAN-01) | planning | model | head#18,head#73 |
| `har81-p-d-candidate-1789-security-appsec__Nhf2HdJ` |  |  |  |  | outcome=environment/harness (R-ENV-02); first=environment/harness (R-ENV-02, recovered=false); stop=agent_timeout; reward=0.0 (scored=True); turns=23 acc={'true': 23, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1789-security-appsec/har81-p-d-candidate-1789-securit__Nhf2HdJ; first environment/harness @ head#7 (R-ENV-02); outcome environment/harness @ head#7 (R-ENV-02) | environment | harness | head#7 |
| `har81-p-d-candidate-1702-ml-inference__izs4jgG` |  |  |  |  | outcome=environment/harness (R-ENV-02); first=environment/harness (R-ENV-02, recovered=false); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=95 acc={'true': 95, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs/har81-p-d-candidate-1702-ml-inference/har81-p-d-candidate-1702-ml-infe__izs4jgG; first environment/harness @ head#2 (R-ENV-02); outcome environment/harness @ head#2 (R-ENV-02) | environment | harness | head#2 |
| `har81-l-d-a3-candidate-1789-security-appsec__WacRiXN` |  |  |  |  | outcome=environment/harness (R-ENV-02); first=environment/harness (R-ENV-02, recovered=false); stop=ceiling:input_tokens; reward=0.0 (scored=True); turns=115 acc={'true': 115, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs/har81-l-d-a3-candidate-1789-security-appsec/har81-l-d-a3-candidate-1789-secu__WacRiXN; first environment/harness @ head#6 (R-ENV-02); outcome environment/harness @ head#6 (R-ENV-02) | environment | harness | head#6 |

## After labeling

- Where your hand tag differs from the proposal, cite the step that changed your mind.
- If 2+ rows blame the task (not the agent), flag the task for the env audit; if rows show the same agent loop twice, note the loop shape for the harness fix.
