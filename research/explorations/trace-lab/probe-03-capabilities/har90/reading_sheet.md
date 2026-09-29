# Reading sheet (hand labeling, HAR-91 proposals)

_Source: har90/capabilities.jsonl; 10 traces balanced across outcome tag x attribution (round-robin, distinct tasks first). Open each trace, read it end to end, then fill one table row and CONFIRM or CORRECT the proposed tag (the last three columns are proposals, not labels)._

## Traces to read

### completion: `har90-mimo-0036-e__pWmMRr8`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: 1.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 83, calls reconstructed: 40, tokens: {'input': 2350970, 'output': 30929}
- first failure: completion/harness (R-COMP-01, step head#35, recovered=false)
- outcome-relevant: completion/harness (R-COMP-01, step head#35)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-e/har90-mimo-0036-e__pWmMRr8`

### completion: `har90-mimo-0036-f__8T3nkCz`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 172, calls reconstructed: 165, tokens: {'input': 6293664, 'output': 19479}
- first failure: tool_use/model (R-TOOL-02, step head#27, recovered=true)
- outcome-relevant: completion/model (R-COMP-02, step head#44)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-f/har90-mimo-0036-f__8T3nkCz`

### context: `har90-mimo-0758-b__85WkohB`

- task: `mimo-v2.6-rl/candidate-0758-ml-inference`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 761, calls reconstructed: 761, tokens: {'input': 22355608, 'output': 24314}
- first failure: tool_use/harness (R-TOOL-01, step trajectory.cont-11.json#2, recovered=false)
- outcome-relevant: context/harness (R-CTX-01, step trajectory.cont-11.json#8)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-b/har90-mimo-0758-b__85WkohB`

### environment: `har90-mimo-0036-c__drbtqaU`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: None (scored=False)
- stop: model_auth_error (AuthenticationError)
- turns read: 0, calls reconstructed: 0, tokens: {'input': 0, 'output': 0}
- first failure: environment/unclear (R-ENV-01, step None, recovered=false)
- outcome-relevant: environment/unclear (R-ENV-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-c/har90-mimo-0036-c__drbtqaU`

### tool_use: `har90-mimo-0036-b__XfPjwNk`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: None (scored=False)
- stop: trial_budget_exhausted (RateLimitError)
- turns read: 200, calls reconstructed: 200, tokens: {'input': 2404842, 'output': 8704}
- first failure: tool_use/unclear (R-TOOL-00, step head#2, recovered=true)
- outcome-relevant: tool_use/harness (R-TOOL-01, step head#3)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-b/har90-mimo-0036-b__XfPjwNk`

### tool_use: `har90-mimo-0036__mWHLdCT`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: None (scored=False)
- stop: trial_budget_exhausted (RateLimitError)
- turns read: 120, calls reconstructed: 127, tokens: {'input': 2389120, 'output': 13478}
- first failure: tool_use/unclear (R-TOOL-00, step head#2, recovered=true)
- outcome-relevant: tool_use/unclear (R-TOOL-00, step head#17)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036/har90-mimo-0036__mWHLdCT`

### context: `har90-mimo-0758-c__tdpbmxw`

- task: `mimo-v2.6-rl/candidate-0758-ml-inference`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 212, calls reconstructed: 365, tokens: {'input': 5517856, 'output': 36375}
- first failure: tool_use/harness (R-TOOL-01, step trajectory.cont-31.json#6, recovered=false)
- outcome-relevant: context/harness (R-CTX-01, step trajectory.cont-31.json#8)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-c/har90-mimo-0758-c__tdpbmxw`

### environment: `har90-mimo-0758-a__TheXwyy`

- task: `mimo-v2.6-rl/candidate-0758-ml-inference`
- reward: None (scored=False)
- stop: model_auth_error (AuthenticationError)
- turns read: 0, calls reconstructed: 0, tokens: {'input': 0, 'output': 0}
- first failure: environment/unclear (R-ENV-01, step None, recovered=false)
- outcome-relevant: environment/unclear (R-ENV-01, step None)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-a/har90-mimo-0758-a__TheXwyy`

### tool_use: `har90-mimo-0758-d__vDCScf3`

- task: `mimo-v2.6-rl/candidate-0758-ml-inference`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 234, calls reconstructed: 60, tokens: {'input': 4824676, 'output': 42932}
- first failure: tool_use/harness (R-TOOL-03, step trajectory.cont-1.json#5, recovered=false)
- outcome-relevant: tool_use/harness (R-TOOL-03, step trajectory.cont-1.json#5)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-d/har90-mimo-0758-d__vDCScf3`

### tool_use: `har90-mimo-0036-d__aN5ARFu`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: 0.0 (scored=True)
- stop: agent_timeout (AgentTimeoutError)
- turns read: 529, calls reconstructed: 532, tokens: {'input': 11106053, 'output': 16626}
- first failure: tool_use/harness (R-TOOL-01, step head#2, recovered=false)
- outcome-relevant: tool_use/harness (R-TOOL-01, step head#2)
- trace: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-d/har90-mimo-0036-d__aN5ARFu`

## Labeling table

| trial | what the agent tried | where it went wrong | failure label (free text) | is the task at fault (y/n/unsure) | evidence line | proposed tag | attribution | evidence steps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `har90-mimo-0036-e__pWmMRr8` |  |  |  |  | outcome=completion/harness (R-COMP-01); first=completion/harness (R-COMP-01, recovered=false); stop=agent_timeout; reward=1.0 (scored=True); turns=83 acc={'true': 33, 'false': 50, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-e/har90-mimo-0036-e__pWmMRr8; first completion/harness @ head#35 (R-COMP-01); outcome completion/harness @ head#35,head#84 (R-COMP-01) | completion | harness | head#35,head#84 |
| `har90-mimo-0036-f__8T3nkCz` |  |  |  |  | outcome=completion/model (R-COMP-02); first=tool_use/model (R-TOOL-02, recovered=true); stop=agent_timeout; reward=0.0 (scored=True); turns=172 acc={'true': 164, 'false': 8, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-f/har90-mimo-0036-f__8T3nkCz; first tool_use/model @ head#27 (R-TOOL-02); outcome completion/model @ head#44,head#55,head#172 (R-COMP-02) | completion | model | head#44,head#55,head#172 |
| `har90-mimo-0758-b__85WkohB` |  |  |  |  | outcome=context/harness (R-CTX-01); first=tool_use/harness (R-TOOL-01, recovered=false); stop=agent_timeout; reward=0.0 (scored=True); turns=761 acc={'true': 0, 'false': 760, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-b/har90-mimo-0758-b__85WkohB; first tool_use/harness @ trajectory.cont-11.json#2 (R-TOOL-01); outcome context/harness @ trajectory.cont-11.json#8 (R-CTX-01) | context | harness | trajectory.cont-11.json#8 |
| `har90-mimo-0036-c__drbtqaU` |  |  |  |  | outcome=environment/unclear (R-ENV-01); first=environment/unclear (R-ENV-01, recovered=false); stop=model_auth_error; reward=None (scored=False); turns=0 acc={'true': 0, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-c/har90-mimo-0036-c__drbtqaU; first environment/unclear @ -- (R-ENV-01); outcome environment/unclear @ -- (R-ENV-01) | environment | unclear | -- |
| `har90-mimo-0036-b__XfPjwNk` |  |  |  |  | outcome=tool_use/harness (R-TOOL-01); first=tool_use/unclear (R-TOOL-00, recovered=true); stop=trial_budget_exhausted; reward=None (scored=False); turns=200 acc={'true': 15, 'false': 185, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-b/har90-mimo-0036-b__XfPjwNk; first tool_use/unclear @ head#2 (R-TOOL-00); outcome tool_use/harness @ head#3,head#32,head#201 (R-TOOL-01) | tool_use | harness | head#3,head#32,head#201 |
| `har90-mimo-0036__mWHLdCT` |  |  |  |  | outcome=tool_use/unclear (R-TOOL-00); first=tool_use/unclear (R-TOOL-00, recovered=true); stop=trial_budget_exhausted; reward=None (scored=False); turns=120 acc={'true': 119, 'false': 1, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036/har90-mimo-0036__mWHLdCT; first tool_use/unclear @ head#2 (R-TOOL-00); outcome tool_use/unclear @ head#17,head#121 (R-TOOL-00) | tool_use | unclear | head#17,head#121 |
| `har90-mimo-0758-c__tdpbmxw` |  |  |  |  | outcome=context/harness (R-CTX-01); first=tool_use/harness (R-TOOL-01, recovered=false); stop=agent_timeout; reward=0.0 (scored=True); turns=212 acc={'true': 4, 'false': 178, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-c/har90-mimo-0758-c__tdpbmxw; first tool_use/harness @ trajectory.cont-31.json#6 (R-TOOL-01); outcome context/harness @ trajectory.cont-31.json#8 (R-CTX-01) | context | harness | trajectory.cont-31.json#8 |
| `har90-mimo-0758-a__TheXwyy` |  |  |  |  | outcome=environment/unclear (R-ENV-01); first=environment/unclear (R-ENV-01, recovered=false); stop=model_auth_error; reward=None (scored=False); turns=0 acc={'true': 0, 'false': 0, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-a/har90-mimo-0758-a__TheXwyy; first environment/unclear @ -- (R-ENV-01); outcome environment/unclear @ -- (R-ENV-01) | environment | unclear | -- |
| `har90-mimo-0758-d__vDCScf3` |  |  |  |  | outcome=tool_use/harness (R-TOOL-03); first=tool_use/harness (R-TOOL-03, recovered=false); stop=agent_timeout; reward=0.0 (scored=True); turns=234 acc={'true': 56, 'false': 177, 'unknown': 1}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0758-d/har90-mimo-0758-d__vDCScf3; first tool_use/harness @ trajectory.cont-1.json#5 (R-TOOL-03); outcome tool_use/harness @ trajectory.cont-1.json#5,trajectory.cont-1.json#6,trajectory.cont-1.json#181 (R-TOOL-03) | tool_use | harness | trajectory.cont-1.json#5,trajectory.cont-1.json#6,trajectory.cont-1.json#181 |
| `har90-mimo-0036-d__aN5ARFu` |  |  |  |  | outcome=tool_use/harness (R-TOOL-01); first=tool_use/harness (R-TOOL-01, recovered=false); stop=agent_timeout; reward=0.0 (scored=True); turns=529 acc={'true': 0, 'false': 529, 'unknown': 0}; trace=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har90-modal-mimo/runs/har90-mimo-0036-d/har90-mimo-0036-d__aN5ARFu; first tool_use/harness @ head#2 (R-TOOL-01); outcome tool_use/harness @ head#2,head#7,head#530 (R-TOOL-01) | tool_use | harness | head#2,head#7,head#530 |

## After labeling

- Where your hand tag differs from the proposal, cite the step that changed your mind.
- If 2+ rows blame the task (not the agent), flag the task for the env audit; if rows show the same agent loop twice, note the loop shape for the harness fix.
