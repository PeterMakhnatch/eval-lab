# seeker-sft-v0 — verified seeker trajectories (empty in v0)

`train.jsonl` holds successful reward-seeker trajectories from
seeker-gym-v0: prompt + submission that earned verified gym reward
(upstream 1, fixed 0). **It is empty in v0.**

First rejection-sampling round (2026-10-21): GPT-OSS-120b, K=8 on 4
TRAIN tasks (atrx-vep-crispr, batched-eval-parity, cad-model,
cargo-flight-dispatch) = 32 candidates, generation cost $0.0068,
Docker-graded upstream: **0/32 passed the original grader**, so no
candidate reached the fixed grader and no trajectory is saved. Per-task
success rate: 0/8 on all four tasks. Full candidate log:
`research/experiments/seeker-gym-v0/runs/results.jsonl`.

Why so low (observed, not excused): one-shot `solve.sh` submissions from
a single prompt rarely satisfy the upstream grader at all — most
candidates either do too little (empty/placeholder outputs) or aim at
grader files the submission cannot actually replace. The gym's value in
v0 is the verified pipeline and the negative measurement; an agentic
seeker (tool use, execution feedback) is the expected way to get the
first successes, as in tb4-reward-seeker-v0 where only 1/71 candidates
was replay-confirmed.

No training is authorized or started from this dataset. Licence follows
the source tasks (TB4 DEV, PM port); no tb4-audit-v1 TEST data is
included — the gym's runnable family is DEV only.
