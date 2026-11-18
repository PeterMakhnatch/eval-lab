---
status: living
audience:
  - runner
  - operator
  - analyst
---

# Jobs verify: reward integrity for finished Harbor jobs

`evallab jobs verify` checks finished Harbor job directories before any reward
in them is quoted. It runs no task, calls no model, and writes nothing.

Harbor stores the same reward in up to three places: the trial `result.json`
(`verifier_result.rewards`), the `verifier/reward.json` file, and the job-level
aggregate (`stats.evals`). No checksum binds the copies. Readers also disagree
on edge values: a boolean or `-1` reward is unscored, never a pass or a fail
(HAR-446, HAR-455). This command fails closed when the copies disagree.

```bash
uv run evallab jobs verify runs/<job-name>
uv run evallab jobs verify runs --json
```

Exit codes: `0` pass, `1` block (do not quote a reward from a blocked job),
`2` error (no readable Harbor job). The JSON report uses schema
`evallab.jobs.verify/v1`. Implementation: `src/evallab/job_verify.py`
(stdlib only); CLI registration: `src/evallab/jobs_parser.py`.

## Checks

| Severity | Code | Meaning |
|---|---|---|
| block | `reward_bool`, `reward_non_numeric`, `reward_non_finite`, `reward_unscored_sentinel` | The raw reward value is unscored |
| block | `reward_missing` | No reward in `result.json` or the verifier reward file |
| block | `reward_mismatch_result_vs_file` | Verifier reward file disagrees with `result.json` |
| block | `job_stats_reward_desync`, `job_stats_trial_count_desync` | Job aggregate does not match rewards recomputed from trials |
| block | `trial_count_mismatch`, `trial_result_missing`, `trial_duplicate_id`, `trial_duplicate_name` | Trial set on disk does not match the job declaration |
| block | `job_unfinished`, `job_trial_count_invalid`, `job_stats_missing_evals` | Job record is incomplete |
| warn | `reward_missing_exceptioned` | Trial exceptioned before grading; it has no quotable reward |
| warn | `reward_file_orphan` | Only the verifier reward file holds a reward |
| info | `pass_at_k_empty` | Harbor leaves `pass_at_k` empty for multi-dimension or non-binary rewards |
| info | `task_digest_namespace` | Lock task digests, reported as opaque strings |

Two rules need care:

- Harbor counts only scored trials in `stats.evals`. An exceptioned trial with
  no reward raises `n_total_trials` and `n_errored_trials` but no eval count.
  The verifier follows that rule; it does not flag the difference.
- A trial `task_checksum` and a lock `task.digest` are different digest
  namespaces. They differ on the same trial. Never join or compare them.

First full local run (2026-11-18): 164 jobs under `runs/`, 164 pass, 22 warn
findings (all `reward_missing_exceptioned`), 0 block findings.
