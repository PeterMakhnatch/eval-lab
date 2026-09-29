# Reading sheet (hand labeling)

_Source: metrics.jsonl. Open each trace, read it end to end, then fill one table row. Keep labels free text -- no fixed taxonomy yet._

_Note: no trials available for: infra error (slot skipped, not faked)._

## Traces to read

### pass: `event-summary__5E3btLv`

- task: `local-lab/event-summary`
- reward: 1.0 (scored=True)
- turns: 5, calls: 4, tokens: 72190
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/agent/trajectory.json`

### fail #1 (same task): `terminal-bench-html-js-filter__5rgjEEt`

- task: `terminal-bench/html-js-filter`
- reward: 0.0 (scored=True)
- turns: 16, calls: 15, tokens: 399780
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt/agent/trajectory.json`

### fail #2 (same task): `terminal-bench-html-js-filter__D3GZpFU`

- task: `terminal-bench/html-js-filter`
- reward: 0.0 (scored=True)
- turns: 13, calls: 12, tokens: 319280
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__D3GZpFU/agent/trajectory.json`

### longest run (already listed): `terminal-bench-html-js-filter__5rgjEEt`

- task: `terminal-bench/html-js-filter`
- reward: 0.0 (scored=True)
- turns: 16, calls: 15, tokens: 399780
- trace: `/Users/petermakhnatch/Developer/eval-lab/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt/agent/trajectory.json`

## Labeling table

| trial | what the agent tried | where it went wrong | failure label (free text) | is the task at fault (y/n/unsure) | evidence line |
| --- | --- | --- | --- | --- | --- |
| `event-summary__5E3btLv` |  |  |  |  | reward=1.0 turns=5 calls=4 across_repeat=0.0; trace=/Users/petermakhnatch/Developer/eval-lab/runs/canary-event-summary-codex-20260815/event-summary__5E3btLv/agent/trajectory.json |
| `terminal-bench-html-js-filter__5rgjEEt` |  |  |  |  | modes=unclassified_failure (src=evallab.trial_diagnosis); turns=16 calls=15 across_repeat=0.0667; trace=/Users/petermakhnatch/Developer/eval-lab/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt/agent/trajectory.json |
| `terminal-bench-html-js-filter__D3GZpFU` |  |  |  |  | modes=unclassified_failure (src=evallab.trial_diagnosis); turns=13 calls=12 across_repeat=0.0; trace=/Users/petermakhnatch/Developer/eval-lab/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__D3GZpFU/agent/trajectory.json |

## After labeling

- If 2+ rows blame the task (not the agent), flag the task for the HAR-82/HAR-83 env audit.
- If rows show the same agent loop twice, note the loop shape (exact-repeat calls? silent outputs?) for the harness fix.
