# Reading sheet (hand labeling)

_Source: metrics-t2.jsonl. Open each trace, read it end to end, then fill one table row. Keep labels free text -- no fixed taxonomy yet._

_Note: no trials available for: fail #1, fail #2 (slot skipped, not faked)._

## Traces to read

### pass: `fix-slugify__nNLTZFY`

- task: `fix-slugify`
- reward: 1.0 (scored=True)
- turns: 13, calls: 12, tokens: 35202
- trace: `/Users/petermakhnatch/Developer/research-context/reef/experiments/work/05/view/r1-check-candidate/fix-slugify__nNLTZFY/agent/trajectory.json`

### infra error: `inventory-value__f37iigQ`

- task: `inventory-value`
- reward: None (scored=False)
- turns: 4, calls: 4, tokens: 6017
- trace: `/Users/petermakhnatch/Developer/research-context/reef/experiments/work/05/view/r1-check-candidate/inventory-value__f37iigQ/agent/trajectory.json`

### longest run: `fix-slugify__sbofByt`

- task: `fix-slugify`
- reward: 1.0 (scored=True)
- turns: 20, calls: 16, tokens: 68166
- trace: `/Users/petermakhnatch/Developer/research-context/reef/experiments/work/05/view/r1-check-candidate/fix-slugify__sbofByt/agent/trajectory.json`

## Labeling table

| trial | what the agent tried | where it went wrong | failure label (free text) | is the task at fault (y/n/unsure) | evidence line |
| --- | --- | --- | --- | --- | --- |
| `fix-slugify__nNLTZFY` |  |  |  |  | reward=1.0 turns=13 calls=12 across_repeat=0.3333; trace=/Users/petermakhnatch/Developer/research-context/reef/experiments/work/05/view/r1-check-candidate/fix-slugify__nNLTZFY/agent/trajectory.json |
| `inventory-value__f37iigQ` |  |  |  |  | exception=InternalServerError reward=None; trace=/Users/petermakhnatch/Developer/research-context/reef/experiments/work/05/view/r1-check-candidate/inventory-value__f37iigQ/agent/trajectory.json |
| `fix-slugify__sbofByt` |  |  |  |  | reward=1.0 turns=20 calls=16 across_repeat=0.125; trace=/Users/petermakhnatch/Developer/research-context/reef/experiments/work/05/view/r1-check-candidate/fix-slugify__sbofByt/agent/trajectory.json |

## After labeling

- If 2+ rows blame the task (not the agent), flag the task for the HAR-82/HAR-83 env audit.
- If rows show the same agent loop twice, note the loop shape (exact-repeat calls? silent outputs?) for the harness fix.
