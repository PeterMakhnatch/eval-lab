# trajectory-judge-400: injected-fault agent trajectories (QA Agent - Trace, method key)

`qa.answer_key/v1`, built by `build.py` (2026-10-14).

## Source and licence

- Upstream: <https://github.com/mohammadi-hadi/trajectory-judge>. MIT, © 2026 Hadi
  Mohammadi; `upstream/LICENSE`. Pinned at commit
  `056eb6c78012028dbc499841f6d89c8a29b91eb3` (PyPI `trajectory-judge==0.2.0`).
- `upstream/` holds unmodified copies of upstream `data/trajectories.jsonl`,
  `data/verdicts.jsonl`, `data/run.json`, `data/PROVENANCE.md` and
  `analysis/paired.json`. `build.py` checks their md5s against upstream
  `PROVENANCE.md` before writing anything.
- Domain: a synthetic customer-support tool environment (refunds, policies,
  escalation). It is not a code sandbox. Use it to check method (step-level fault
  detection, paired discrimination, calibration), not as evidence about our domain.

## Items and labels

400 items, one per trajectory:

| upstream label | n | `expected_category` | `expected_verdict` |
|---|---|---|---|
| clean | 100 | `false_positive` (the question a clean pass answers "no" to) | `pass` |
| silent fault (`outcome_correct: true`): hallucinated_argument 50, unsupported_claim 50, wrong_tool 50, skipped_precondition 25 | 175 | `false_positive` | `fail` |
| loud fault (`outcome_correct: false`): ignored_observation 50, premature_stop 50, skipped_precondition 25 | 125 | `failure_cause` | `fail` |

- Faulty items carry `expected_root_cause{failure_type, failure_step}` from upstream.
- Labels are upstream's injected faults: the generator knows where it broke each
  trajectory.
- `pairs.jsonl` has 251 pairs. Each pairs a faulty trajectory with the clean
  trajectory of the same `instance_id`.
- The other 49 faulty trajectories have their clean parent only in upstream's
  ablation file, which is not imported.
- Split: `dev` (212) / `test` (188), from a seeded hash of `instance_id`. Every
  trajectory of an instance lands in the same split.
- Item-level assignments are in `../splits/assignments.csv`.
- The qa-trace/3 prompts were tuned on 28 items, 15 of them `test`, listed in
  `research/experiments/qa-trace/tuned_on/trajectory-judge-400.txt`. This is exposure
  `qa-trace-tj-prompt-dev` in `../splits/exposure.jsonl`.
  - A QA Trace `test` row must leave those 15 items unmeasured, for example with
    `--exclude`, or `splits/guard.py --check` fails.
  - Any new prompt work belongs on `dev` only. If it touches more test items, add
    them to the tuned-on file first.
- There are no graders here, so "regrading" this key means re-judging. The same
  exclusion applies.

## Baseline

Run `uv run python research/qa-scoreboard/combined_v1_baseline.py`. Numbers below are
on all items.

| system | faults caught | clean flagged | paired Δ | type match on catches |
|---|---|---|---|---|
| `combined_v1@deploy-agentic` (rules + content legs; Sol leg not run) | 0/300 | 0/100 | 0.00 | n/a |
| reference: upstream `programmatic` | 200/300 | 0/100 | 0.60 | 1.00 |
| reference: upstream `step:qwen2.5:14b` | 257/300 | 0/100 | 0.83 | 0.71 |
| reference: upstream `selfcons3:qwen2.5:14b` | 253/300 | 1/100 | 0.81 | 0.71 |
| reference: upstream `outcome:qwen2.5:14b` | 184/300 | 33/100 | 0.16 | 0.54 |
| reference: upstream `step:llama3.1:8b` | 297/300 | 100/100 | −0.01 | 0.40 |

**combined_v1 rows.**
- combined_v1's literals look for sandbox cheats: hidden-file reads, grader
  tampering, upstream fetches, test-harness code. A customer-support tool call has
  nothing for them to match, so 0 of 400 fire.
- The Sol leg has no cached verdicts here. As deployed, it is a cascade that treats
  an unjudged trace as not flagged.
- So this row is a measured floor for the rules and content legs, not a full
  combined_v1 score.

**Reference rows.**
- These are upstream's five judges, recomputed offline from its released raw
  verdicts. They are not ours.
- They also check the scorer against upstream's own results:
  - Clean flags match `analysis/paired.json` `clean_flags`: 0, 0, 1, 33 and 100.
  - Detected counts match `localisation_counts`: 200, 257, 253 and 297.
  - The outcome judge's paired Δ is 0.393 on loud faults and 0.048 on silent
    faults. The difference, 0.345, matches upstream's
    `outcome_paired_loud_minus_silent` (0.344953).
