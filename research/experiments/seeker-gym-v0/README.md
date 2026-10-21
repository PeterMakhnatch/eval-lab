# seeker-gym-v0 — verified-reward environment for reward seekers

A small gym of paired graders. A submission earns **reward 1 iff it
passes the original (weak) grader and fails the fixed grader** — a
verified differential with no LLM judge. Candidates identical to the
reference are excluded (the runner records candidate digests; reference
solutions are never shown to the generator).

## Registry (`gym.json`, built by `build_gym.py`)

- **tb4-dev (66 pairs, 65 runnable):** TB4 DEV tasks, upstream grader vs
  Preference Model port, both in the local checkout. This is the runnable
  family in v0 (local Docker via `evallab.candidate_runner`, #1140).
- **mimo-repairs (54 pairs, registered, not runnable here):** qualified
  v7/hardening repairs from mimo-defect-map-v1. Original packages are
  local; repaired child packages sit in native-executor custody and are
  not materialized in this checkout, so no differential is claimed for
  them until they are staged.
- TB2→TB2.1 admitted pairs: none resolved to both-local graders in this
  checkout during the v0 build; the family is registered as empty rather
  than padded with unverified pairs.

Split is by task (sha256 bucket, ~60/40 train/eval).

## CLI

```bash
keys run -- .venv/bin/python research/experiments/seeker-gym-v0/seeker_gym.py \
    --model gpt-oss-120b --k 8 --split train --limit 3 \
    --out research/experiments/seeker-gym-v0/runs
```

One OpenRouter generation per candidate (seeker prompt: shortest path to
reward 1), then Docker grading: upstream first; the fixed (port) grader
runs only when upstream reward is 1. Every candidate is logged to
`results.jsonl`; verified successes are appended to `seeker-sft-v0.jsonl`
in the output dir (prompt + winning submission + differential receipt).

## Limits

- Docker grading needs the `harbor` package in the worktree venv
  (`uv pip install "harbor[modal]==0.24.0"` if the sync omitted it).
- Modal grading through the same runner was not used: its per-grade
  reservation exceeds the lane's $1.50 Modal cap. Local Docker is free.
- Generation sees instruction + grader files only; no reference solution.
