# Data provenance

`data/` holds unmodified copies of `results/raw`, `results/agent` and `results/ablation` under
flat names. The scripts in `analysis/` read only this directory, and CI fails if a copy differs
from its source.

| File | Source |
|---|---|
| `verdicts.jsonl`, `trajectories.jsonl`, `run.json` | `results/raw/` |
| `agent_trajectories.jsonl` | `results/agent/trajectories.jsonl` |
| `ablation_verdicts.jsonl`, `ablation_trajectories.jsonl`, `ablation_responses.jsonl`, `ablation_run.json` | `results/ablation/raw/` |
| `ablation_engine.txt` | `results/ablation/engine.txt` |
| `organic_verdicts.jsonl`, `organic_responses.jsonl`, `organic_run.json` | `results/ablation/organic/` |

## The August runs (release v0.1.0)

The five judges of the main comparison, from commit 16c7502. The model verdicts came from
Ollama 0.30.11 (server log of 4 August 2026), `qwen2.5:14b` and `llama3.1:8b`.

## The October runs (view-by-task ablation, agent episodes)

One session on 1 and 2 October 2026, Ollama 0.33.3, `qwen2.5:14b`, code at commit 160620d.
`ablation_trajectories.jsonl` holds the 400 benchmark trajectories plus the 41 clean parents
they lack, with `premature_stop` and `unsupported_claim` first.

`results/ablation/queue.sh` is what ran. It logs the engine version and the model digest
before and after every step; the digest is 7cdf5a0187d5 throughout, and
`results/ablation/PREDICTIONS.md` names the weights blob (sha256:2049f5674b1e). During the runs
the log was called `ENV.log`; it was renamed `engine.txt` when committed (b92ab5f), because
`*.log` files are ignored. Its `code=` line is the path of the frozen worktree the runs used,
checked out at 160620d.

Predictions and decision rules: `results/ablation/PREDICTIONS.md` (c16c2eb, amended in 160620d
before the queue started; the file says what the amendment was).

## Checksums (md5)

```
8ee883f0fbc105197046658b719ae6d3  agent_trajectories.jsonl
e76bb949d2860a2859f754866a9a14ec  trajectories.jsonl
31c04822dc3258b081b254e446181f88  verdicts.jsonl
dca82a7d83e7ab0effa809775ec0ce01  run.json
b29331f2f258f1968e8a16b90b5b3830  ablation_responses.jsonl
82df644a1fedfa0aff7fda5bfd3d8bd5  ablation_trajectories.jsonl
2bcc34b14ea31c661801b9c4c82c978b  ablation_verdicts.jsonl
dc67744bfdcee2aa60af62d451571832  ablation_run.json
eff10566f6e4392928a73a152a3df55b  ablation_engine.txt
9e0cd142dca7696042db720ff9d02218  organic_responses.jsonl
d4d2737e565561ae9c8df977582ff861  organic_run.json
a466c9c785bde68e122515cef7b52c0f  organic_verdicts.jsonl
```
