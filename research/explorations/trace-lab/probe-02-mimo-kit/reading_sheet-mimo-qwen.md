# Reading sheet (hand labeling)

_Source: metrics-mimo-qwen.jsonl. Open each trace, read it end to end, then fill one table row. Keep labels free text -- no fixed taxonomy yet._

_Note: no trials available for: pass (slot skipped, not faked)._

## Traces to read

### fail #1 (same task): `candidate-0036-software-data-eng__PJpwYiu`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: 0.0 (scored=True)
- turns: 10, calls: 46, tokens: 199816
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/har83-qwen-terminal-0036/candidate-0036-software-data-eng__PJpwYiu/agent/trajectory.json`

### fail #2 (same task): `candidate-0036-software-data-eng__wFhM6fV`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: 0.0 (scored=True)
- turns: 10, calls: 65, tokens: 206697
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/har83-qwen-terminal-0036/candidate-0036-software-data-eng__wFhM6fV/agent/trajectory.json`

### infra error: `har83-blocklist-proof-cyber__mFRAH92`

- task: `har83-blocklist-proof-cyber`
- reward: None (scored=False)
- turns: 5, calls: 26, tokens: 73649
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/har83-blocklist-proof-cyber/har83-blocklist-proof-cyber__mFRAH92/agent/trajectory.json`

### longest run (already listed): `candidate-0036-software-data-eng__wFhM6fV`

- task: `mimo-v2.6-rl/candidate-0036-software-data-engineering`
- reward: 0.0 (scored=True)
- turns: 10, calls: 65, tokens: 206697
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/har83-qwen-terminal-0036/candidate-0036-software-data-eng__wFhM6fV/agent/trajectory.json`

## Labeling table

| trial | what the agent tried | where it went wrong | failure label (free text) | is the task at fault (y/n/unsure) | evidence line |
| --- | --- | --- | --- | --- | --- |
| `candidate-0036-software-data-eng__PJpwYiu` |  |  |  |  | modes=tool_use_loop,state_persistence_assumption,planning_no_edit (src=evallab.trial_diagnosis); turns=10 calls=46 across_repeat=0.6522; trace=/Users/petermakhnatch/Developer/eval-lab/runs/har83-qwen-terminal-0036/candidate-0036-software-data-eng__PJpwYiu/agent/trajectory.json |
| `candidate-0036-software-data-eng__wFhM6fV` |  |  |  |  | modes=planning_no_edit (src=evallab.trial_diagnosis); turns=10 calls=65 across_repeat=0.7231; trace=/Users/petermakhnatch/Developer/eval-lab/runs/har83-qwen-terminal-0036/candidate-0036-software-data-eng__wFhM6fV/agent/trajectory.json |
| `har83-blocklist-proof-cyber__mFRAH92` |  |  |  |  | reward=None turns=5 calls=26 across_repeat=0.1154; trace=/Users/petermakhnatch/Developer/eval-lab/runs/har83-blocklist-proof-cyber/har83-blocklist-proof-cyber__mFRAH92/agent/trajectory.json |

## After labeling

- If 2+ rows blame the task (not the agent), flag the task for the HAR-82/HAR-83 env audit.
- If rows show the same agent loop twice, note the loop shape (exact-repeat calls? silent outputs?) for the harness fix.
