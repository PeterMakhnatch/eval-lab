# Package anatomy against executed health classes (HAR-510)

Date: 2026-11-26. Cost: **$0**. **No task was executed**.
This join runs the read-only static package anatomy on a corpus clone and the committed
executed health card. It uses no Docker, no network, no model calls, and no Modal.
It is the anatomy counterpart of the task-lint join in
[`../task-lint-vs-executed-health/`](../task-lint-vs-executed-health/README.md) (HAR-454).

Source commit: the CSV was built and checked at eval-lab commit `e61c1dca4`
(`origin/main` at build time). The anatomy source `src/evallab/package_anatomy.py`
last changed at `b62d5bae0` (HAR-501).

## The answer

The health card has 95 tasks: 49 `clean` and 46 not clean (51.6% clean).
Anatomy reports **353 hypotheses** with **16 distinct codes** on the same 95 task packages.
Two codes separate the classes strongly. They are two views of one test-file shape:

| Code | Tasks | `clean` | Not clean | Clean share | Fisher two-sided p |
|---|---:|---:|---:|---:|---:|
| `WC-TINY-SUITE` | 37 | 3 | 34 | 8% | 4.5e-12 |
| `EG-EXIT-CODE-ONLY-GATE` | 54 | 45 | 9 | 83% | 1.8e-13 |

- `WC-TINY-SUITE` flags mostly not-clean tasks.
- `EG-EXIT-CODE-ONLY-GATE` flags mostly clean tasks.
- The two codes fire together on 1 task only, `sign-vector-game` (`clean`).
- `EG-EXIT-CODE-ONLY-GATE` fires on 54 of the 58 tasks without the grader wrapper (below)
  and on 0 of the 37 tasks with it.

Weaker one-sided codes, each on 3 tasks or fewer:

- `WC-NO-TESTS-FOUND` flags 3 tasks, all not clean (p = 0.11).
- `AR-GIT-HISTORY` and `GT-AUTOLOAD-WRITABLE` flag 1 not-clean task each.
- `EG-FAILURE-MASKED` flags 1 clean task.

The two most frequent codes separate nothing:
`GT-PATH-HIJACK` fires on 94 of 95 tasks and `GT-SITE-PACKAGES-HOOK` on 84 of 95,
both at the base clean share of 52%.

## The grader-wrapper shape behind `WC-TINY-SUITE`

37 tasks have a `tests/test_outputs.py` that is a thin wrapper around a separate grader.
It has one test function, `test_grader_validation`, with this shape:

```python
from grader import grade
result = grade()
# write result.score to /logs/verifier/reward.txt and detailed_scores.json
assert True, f"Grader completed with score: {result.score}"
# on exception: write 0.0 to reward.txt and pytest.fail(...)
```

The reward comes from `result.score`, which Python writes to `reward.txt`.
The pytest exit code does not set the reward, so `EG-EXIT-CODE-ONLY-GATE` stays quiet.
The parsed suite has 1 test and 1 assertion (`assert True`), so `WC-TINY-SUITE` fires.
The real checks live in `tests/grader.py`. The anatomy parses that file too,
but it counts only `test_*` functions there, and most graders define none.

The wrapper split by executed class:

| Shape | Tasks | `clean` | `reference_below_one` | `reference_unmeasured` | `no_reference` | `cannot_run` |
|---|---:|---:|---:|---:|---:|---:|
| Grader wrapper | 37 | 2 | 6 | 3 | 26 | 0 |
| Other | 58 | 47 | 3 | 7 | 0 | 1 |

`WC-TINY-SUITE` fires on 36 of the 37 wrapper tasks, including 25 of the 26 `no_reference` tasks.
The exception is `grpc-plant-position-server` (`no_reference`):
its `tests/grader.py` defines 6 `test_*` functions, so the suite counts 7 tests.
The one non-wrapper `WC-TINY-SUITE` task is `sign-vector-game` (`clean`),
which has 1 real test with 1 assertion.
The 3 clean `WC-TINY-SUITE` tasks are `anomaly-detection-ranking`, `sales-data-csv-analysis`,
and `sign-vector-game`.

**This separation is a counting shape, not evidence that tiny suites fail.**
`WC-TINY-SUITE` counts tests and assertions in the wrapper file.
It does not read the grader that sets the reward.
All 26 `no_reference` tasks use the wrapper.
The code therefore tracks how the import packaged these tasks, not how strong their checks are.
`no_reference` means the package has no reference solution to execute, not that the grader is weak.

## Data and join

Inputs:

- Card: [`research/audits/corpus-health-tblite-verified-2026-11-15.json`](../../audits/corpus-health-tblite-verified-2026-11-15.json), 95 `rows[]` with `task` and `class`.
- Corpus: `git clone https://github.com/huggingface/terminal-bench-lite-verified /tmp/tblv` at commit `f8f4fc8f738d4819eba8eb88bb5033c58e7ec172`.

The join key is the card task name. It must equal a task directory name in the clone,
and that directory must hold `task.toml`. All 95 card tasks resolve, and the card has
no duplicate task.

## Method

The builder imports `evallab.package_anatomy.analyze_package` and calls it on each joined
task directory. It reads `vulnerability_hypotheses`. It does not run the task, execute a
container, or write into the clone.

For each task the CSV `anatomy-health.csv` records:

- `task`: the exact card task name and directory name.
- `executed_class`: the card class (`clean`, `reference_below_one`, `reference_unmeasured`, `no_reference`, `cannot_run`).
- `hypothesis_codes`: unique codes as `CODE:score`, sorted by code, joined by `;`.
  The code is the hypothesis `id` without a `#n` suffix. A repeated code keeps its highest score.
- `hypothesis_count`: every hypothesis, including repeated codes.

Rows are in task-name order. The CSV has 95 data rows and 96 lines, with LF endings.
Reproduce from the repository worktree:

```bash
git clone https://github.com/huggingface/terminal-bench-lite-verified /tmp/tblv
git -C /tmp/tblv checkout f8f4fc8
uv run python research/experiments/anatomy-vs-executed-health/build.py
uv run python research/experiments/anatomy-vs-executed-health/build.py --check
```

`--check` compares exact bytes and writes nothing. It exits 0 when the CSV is current,
1 on drift or a missing CSV, and 2 on unusable inputs. `TBLV_CORPUS_ROOT` and
`TBLV_HEALTH_CARD` override the default roots; `--out-dir` writes elsewhere.
A change to the anatomy source can change the CSV; then `--check` exits 1, and the
builder must run again and this README must record the new source commit.

## Hypothesis codes by executed class

Cells count flagged tasks. A task counts once per code, even when the code repeats.
"Not clean" is the sum of the four non-clean classes.
The p value is an unadjusted two-sided Fisher exact test of clean versus not clean
against the 49/46 split. It describes this table only; 16 codes were compared.

| Code | Tasks | `clean` | Not clean | `reference_below_one` | `reference_unmeasured` | `no_reference` | `cannot_run` | Scores | p |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| `GT-PATH-HIJACK` | 94 | 49 | 45 | 9 | 9 | 26 | 1 | 35 | 0.48 |
| `GT-SITE-PACKAGES-HOOK` | 84 | 44 | 40 | 7 | 6 | 26 | 1 | 40 | 0.75 |
| `EG-EXIT-CODE-ONLY-GATE` | 54 | 45 | 9 | 2 | 6 | 0 | 1 | 50 | 1.8e-13 |
| `WC-INSTRUCTION-SYMBOL-UNTESTED` | 37 | 24 | 13 | 2 | 4 | 7 | 0 | 45–75 | 0.058 |
| `WC-TINY-SUITE` | 37 | 3 | 34 | 6 | 3 | 25 | 0 | 40 | 4.5e-12 |
| `AR-TESTS-IN-IMAGE` | 9 | 4 | 5 | 1 | 0 | 3 | 1 | 85–92 | 0.74 |
| `EG-ARTIFACT-ONLY-CHECK` | 7 | 6 | 1 | 0 | 1 | 0 | 0 | 72 | 0.11 |
| `WC-SOURCE-TEXT-ASSERT` | 6 | 5 | 1 | 1 | 0 | 0 | 0 | 65 | 0.21 |
| `EG-INSTRUCTION-RULE-UNENFORCED` | 5 | 3 | 2 | 0 | 1 | 1 | 0 | 60 | 1 |
| `WC-TEST-WITHOUT-ASSERTION` | 5 | 4 | 1 | 0 | 0 | 1 | 0 | 55 | 0.36 |
| `WC-NO-NEGATIVE-CASES` | 4 | 2 | 2 | 0 | 0 | 2 | 0 | 50 | 1 |
| `GT-BAKED-TESTS-EXECUTED` | 3 | 1 | 2 | 0 | 1 | 0 | 1 | 95 | 0.61 |
| `WC-NO-TESTS-FOUND` | 3 | 0 | 3 | 1 | 2 | 0 | 0 | 45 | 0.11 |
| `AR-GIT-HISTORY` | 1 | 0 | 1 | 0 | 1 | 0 | 0 | 65 | 0.48 |
| `EG-FAILURE-MASKED` | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 85 | 1 |
| `GT-AUTOLOAD-WRITABLE` | 1 | 0 | 1 | 0 | 0 | 0 | 1 | 80 | 0.48 |

Notes on single tasks:

- `GT-PATH-HIJACK` misses only `okhttp-trailers-crash` (`reference_unmeasured`).
- `WC-NO-TESTS-FOUND` flags `okhttp-trailers-crash`, `protein-sequence`, and `smiles-data-lab`.
- 4 non-wrapper tasks lack `EG-EXIT-CODE-ONLY-GATE`: `jq-data-processing` and
  `service-deployment-wave-planner` (`clean`), `protein-sequence`, and `smiles-data-lab`.

## Hypotheses by executed class

| Class | Tasks | Hypotheses | Mean per task | Min | Max |
|---|---:|---:|---:|---:|---:|
| `clean` | 49 | 192 | 3.92 | 2 | 6 |
| `reference_below_one` | 9 | 29 | 3.22 | 2 | 5 |
| `reference_unmeasured` | 10 | 34 | 3.40 | 2 | 6 |
| `no_reference` | 26 | 92 | 3.54 | 3 | 5 |
| `cannot_run` | 1 | 6 | 6.00 | 6 | 6 |
| **Total** | **95** | **353** | **3.72** | **2** | **6** |

Every task has at least 2 distinct codes. No task has zero hypotheses.

## Limits

- Anatomy hypotheses are descriptive, not keep/fix/discard predictions.
  This README reports counts only.
- A class comes from one executed reference and one nop cell. It measures solvability and
  nop payoff, not whether a grader accepts a wrong answer. A hypothesis about an
  exploitable grader can be true on a `clean` task.
- The two separating codes split on the grader-wrapper shape. The shape is confounded with
  the `no_reference` class, so the split does not show that either code predicts health.
- The join measures this card snapshot, this corpus commit, and the anatomy source at the
  recorded commit. A later card, corpus, or anatomy change can change rows.
- The scan reads source bytes only. It does not build an image or run a shell file.
