# TB4 reader baseline: how many of PM's findings does a plain read find, and does the leaderboard help?

Bridge: `research-context/bridge/2026-10-20-open-qa/run-census.md` (pA-SELF-2). Written before any
reader output exists; the prompt (`reader_prompt.md`) is frozen with it.

## Question

1. **Baseline.** If a model only reads a task's exported files (no tools, no execution), what share of
   the key's task findings does it find, per defect category and split? A tool-using QA agent has to
   beat this number to justify its harness.
2. **Does public data help?** Same reader, same tasks, but also given a summary of how 35 public
   leaderboard rows failed on the task (`failure-evidence.md`). Does recall go up, and does precision
   go down?

## Design

- Tasks: all 66 in `tb4-audit-v1` (25 dev, 41 test). The reader is not trained or tuned on any of it,
  so reporting dev and test together is fair; both are reported.
- Reader: `anthropic/claude-opus-5-5` subagents, fresh context, 6 tasks each, opaque task ids, one
  frozen prompt for both arms. Arm `static` sees the exported GT-1 task view. Arm `leaderboard`
  sees the same view plus `failure-evidence.md` (public trials only; no key, no PM text). Arms use
  different batch shuffles and different agents, so no agent sees both arms.
- Output: findings as `qa.finding/v1` rows, scored by `research/answer-keys/tb4-audit-v1/score_audit.py`
  (1-to-1 bipartite matching, task-collision inflation removed).
- No prompt or converter change after the first scored output. A format-only fix (JSON validity) is
  allowed and recorded.

## Declared metrics

- **Primary:** `category_1to1` recall on all 66 tasks, per arm (matched findings of 238), with a Wilson
  95% interval; paired comparison (leaderboard minus static) by bootstrap over tasks, 10,000 resamples,
  seed 20261020. A difference whose interval excludes 0 is called a difference; otherwise no evidence.
- Also: `locus_and_class` recall (stricter: cites the port test's file or symbol and names the class),
  recall by category and by split, and the task-level share of tasks with at least one claimed defect.
- **Precision is a lower bound:** unmatched predictions may be real defects PM did not list. A seeded
  sample of unmatched high-confidence predictions goes to a refutation pass (as in
  `tb4-failure-clusters-20261020`) to estimate how many are real.
- Baselines for context: the scorer's registered trivial baselines (`--benchmark-baselines`).

## Post hoc addition (declared after the first scored output, before any semantic matching ran)

First scored output (declared metrics, `report.md`): the static reader matches 121/238 key findings at
`category_1to1`, but that mode only asks for the same category on the same task, and `locus_and_class`
matches 4/238 because it needs the reader to cite a Karotte-port test file or symbol, which the reader
never sees. Neither says the reader found the same defect. So the declared primary is reported as
declared, and a semantic match is added as the measure to read:

- **Semantic recall.** For each task, an Opus judge sees the key's findings (id, title, class) with
  PM's text for the task, and two anonymised lists of reader findings (A and B, arm order random per
  task). It builds a 1-to-1 matching per list: `same` (the reader finding identifies the same fault, same
  place and mechanism, whatever its class label), `partial` (same area, different mechanism) or none.
  Semantic recall = key findings matched `same` / 238 per arm; paired bootstrap over tasks as above.
  Findings the judge matches only `partial` are reported separately and not counted.

A planned third step, a refutation pass over a seeded sample of unmatched high-confidence reader findings
(to estimate how many are real defects PM did not list), was **not run**: the shared Codex weekly quota
reached the 35% night ceiling at 05:55 ET, and this item stopped spending model calls there. Precision
below is therefore a lower bound only.

## Results

Cost $0 metered. 22 reader subagents (11 per arm, 6 tasks each) and 22 match judges (two independent passes
of 11), all `anthropic/claude-opus-5-5`. 66/66 reader files per arm, no malformed output.

### 1. Declared metrics (`report.md`): the lenient modes say the leaderboard hurts; they do not measure the same defect

| `category_1to1` recall (same category on the same task, 1-to-1) | all 66 | dev | test |
|---|---|---|---|
| static reader | 121/238 = 51% [45, 57] | 50% | 51% |
| leaderboard reader | 107/238 = 45% [39, 51] | 47% | 44% |

Paired difference (leaderboard minus static) −5.9% [−11.5, −0.5]; `task_any_1to1` −8.0% [−14.3, −1.9];
`locus_and_class` 4/238 for both arms, because it needs a citation of a Karotte-port test file or symbol
the reader never sees. `category_1to1` rewards volume (static 251 findings, leaderboard 216) and credits a
category match as a find. The scorer's own trivial baseline of one generic `weak` finding per defective
task gets 13.9% on this mode, so 51% overstates what the reader found.

### 2. Semantic match (`report-semantic.md`, `agreement.md`): read this one

An Opus judge matched each reader finding to the key finding it describes, 1-to-1, `same` only when it names
the same fault in the same place.

| arm | key findings found (same) | precision lower bound | dev | test |
|---|---|---|---|---|
| static | 71/238 = 30% [24, 36] | 71/251 = 28% | 29% | 30% |
| leaderboard | 71/238 = 30% [24, 36] | 71/216 = 33% | 30% | 30% |

- **Paired difference (leaderboard minus static): +0.0% [−3.6, +3.4].** No evidence that the leaderboard
  failure evidence helps a reader find PM's findings, and none that it hurts.
- The two arms find mostly the same findings: 58 in both, 84 (35%) in either. About two thirds of PM's
  findings are missed by a read-only pass, with or without the leaderboard.
- Two independent judge passes (different batching, A/B order reshuffled): the `same` matches are identical
  (142 of 142 reader-to-key pairs), so kappa is 1.00 on `same`; 16 of 132 arm-task cells differ on
  `partial`. Same model and prompt in both passes, so agreement shows the matches are stable, not that the
  judge is unbiased. My spot check of 12 random `same` pairs: 10 clearly the same fault, 2 broad.
- Recall by key category, static / leaderboard: reachable 5/8 and 5/8, tamperable 15/32 (47%) and 12/32
  (38%), weak 30/102 (29%) and 27/102 (26%), broken 7/26 and 9/26, prompt_misaligned 14/70 (20%) and 18/70
  (26%). By PM type: reward_hack 36% / 30%, ambiguity 22% / 30%, grading_or_isolation 24% / 32%.
  Findings PM executed against the upstream grader and confirmed: 6/10 and 5/10.
- Read: a plain read finds exploits (answer reachable, grader tamperable) about twice as often as unstated
  rules. That fits `tb4-failure-clusters-20261020`: unstated-rule findings need run evidence a reader
  cannot get from the files alone. The leaderboard lifted ambiguity findings from 16 to 22 of 74 but
  lowered reward-hack findings from 50 to 42 of 140; neither per-type change was tested, so treat both as
  unconfirmed.

### What a tool-using QA agent has to beat

On the frozen 66 tasks, a no-tools Opus read finds **30% [24, 36] of the key's findings at a 28% precision
lower bound** (3.8 findings per task, 64/66 tasks with a claim). A harness that executes graders and
replays exploits should clear that on executed-exploit findings (`reward_hack`: 36%) and on `ambiguity`,
where the read scores 22%.

## Limits

- One reader model and one judge model family; no human adjudication beyond a 12-pair spot check.
- The key is PM's findings plus Epoch/Tokenless links; a reader finding with no key match may be real.
  Not tested here (see above).
- Both splits were scored once with a frozen prompt. QA-1's agent should treat these numbers as an outside
  reference and not tune on them.

## Reproduce

```bash
uv sync --locked
R=research/experiments/tb4-reader-baseline-20261020
.venv/bin/python $R/build_inputs.py     # 66 views x 2 arms, opaque ids (needs tb4-audit-v1 agent-inputs, hub trials)
# 22 reader subagents with reader_prompt.md, then:
.venv/bin/python $R/score.py            # declared metrics
.venv/bin/python $R/match_inputs.py && .venv/bin/python $R/match_inputs.py 2   # judge packets, passes 1 and 2
# 22 judge subagents with match_prompt.md, then:
.venv/bin/python $R/match_score.py && .venv/bin/python $R/match_score.py 2 && .venv/bin/python $R/match_agreement.py
```
