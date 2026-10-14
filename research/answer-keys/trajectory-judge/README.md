# Answer key: trajectory-judge (`qa.answer_key/v1`, QA Agent - Trace)

**Source.** Mohammadi, *trajectory-judge: What Outcome-Only LLM Judges Miss on Agent
Trajectories* (arXiv 2609.00038), https://github.com/mohammadi-hadi/trajectory-judge at
commit `056eb6c78012028dbc499841f6d89c8a29b91eb3` (`SOURCE.json` pins the sha256 of every
upstream file used). **Licence:** MIT; `trajectories.jsonl` is the upstream file byte for
byte.

**What it labels.** 400 trajectories of a scripted support-desk agent (seven tools: verify
the customer, look up the order, read the policy, confirm eligibility, refund or escalate,
reply): 100 clean oracle runs and 300 runs with one injected fault at a known step, six
types × 50. Labels are exact by construction (fault injection), not human judgments:

| stratum | meaning | items |
|---|---|---:|
| `clean` | oracle run, no fault | 100 |
| `silent` | faulty, but the environment outcome (refund / escalation) stayed correct | 175 |
| `loud` | faulty and the outcome is wrong | 125 |

**Label provenance in `items.jsonl`.** `expected_verdict` is `fail` for every injected
fault (`expected_category: failure_cause`, `expected_fault: agent`) and `pass` for clean
runs. `extra` carries the upstream `failure_step` (step index), `failure_type`,
`outcome_correct`, the `stratum`, and `parent_trial_id`: the clean run the fault was made
from. 49 faults have no clean parent in the released set ("late pairs"); like upstream
`analysis/paired_ci.py` they count toward recall but not toward pairs.

**Split.** Task instances shuffled with seed `qa-trace-20261014`; 10 instances (28
trajectories) are `dev`, used only for prompt iteration; the other 131 instances (372
trajectories) are `heldout`. A fault and its parent are always in the same split.

**Baselines.** `baseline_verdicts.jsonl` holds the paper's five judges' committed
verdicts (rule engine, outcome-only qwen2.5:14b, step-rubric qwen2.5:14b and llama3.1:8b,
self-consistency×3), reduced to the fields the scorer reads, so every score compares
against them on exactly the items judged.

**Scoring.** `evallab qa trace score --key research/answer-keys/trajectory-judge --runs
DIR` reports clean flag rate (false alarms), silent / loud recall, **paired
discrimination** (flag rate on a fault minus flag rate on its own clean parent; 95% CI
resampling task instances) and exact step localisation. Paired discrimination is the
headline: a judge that flags faults and their clean parents alike has detected nothing.

**Use with care.** Synthetic customer-support domain: use it to validate the method
(discrimination, localisation, calibration), not as evidence about coding agents.
Trajectory ids name the fault type (`INS-00010-wrong_tool`), so reviewers only ever see
opaque ids (`qa_trace.blind_id`).

Rebuild: `research/experiments/qa-trace/import_trajectory_judge.py`.
