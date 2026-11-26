# Package anatomy against executed health classes (HAR-510)

Date: 2026-11-26. Cost: **$0**. **No task was executed**.
This join runs the read-only static package anatomy on a corpus clone and the committed
executed health card. It uses no Docker, no network, no model calls, and no Modal.
It is the anatomy counterpart of the task-lint join in
[`../task-lint-vs-executed-health/`](../task-lint-vs-executed-health/README.md) (HAR-454).

Source commit: the CSV was rebuilt and checked at eval-lab commit `671964783`
(`origin/main` at rebuild time, HAR-511). The anatomy source `src/evallab/package_anatomy.py`
last changed at `671964783` (HAR-513), after `88184767c` (HAR-512) and the tiny-suite
change at `60993c77a` (HAR-509).
The first build was at `e61c1dca4`, with the anatomy source at `b62d5bae0` (HAR-501).

## The answer

The health card has 95 tasks: 49 `clean` and 46 not clean (51.6% clean).
Anatomy reports **316 hypotheses** with **16 distinct codes** on the same 95 task packages.
One code separates the classes strongly:

| Code | Tasks | `clean` | Not clean | Clean share | Fisher two-sided p |
|---|---:|---:|---:|---:|---:|
| `EG-EXIT-CODE-ONLY-GATE` | 56 | 45 | 11 | 80% | 4.2e-12 |

- `EG-EXIT-CODE-ONLY-GATE` flags mostly clean tasks.
- It fires on 56 of the 58 tasks without the grader wrapper (below)
  and on 0 of the 37 tasks with it. It therefore also tracks the wrapper shape.
- `WC-TINY-SUITE` now fires on 1 task only, `sign-vector-game` (`clean`, p = 1).

The first build (anatomy at `b62d5bae0`) also reported `WC-TINY-SUITE` as a strong separator:
37 tasks, 3 `clean`, 34 not clean, p = 4.5e-12.
**That separation was a counting shape.** It came from the grader wrapper, not from weak checks.
HAR-509 stops `WC-TINY-SUITE` when a test delegates to a grader module.
The code then left 36 of its 37 tasks, and the separation is gone.

Weaker one-sided codes, each on 3 tasks or fewer:

- `WC-NO-TESTS-FOUND` flags 3 tasks, all not clean (p = 0.11).
- `WC-TEST-WITHOUT-ASSERTION` flags 2 tasks, both `clean` (p = 0.50).
- `AR-GIT-HISTORY` and `GT-AUTOLOAD-WRITABLE` flag 1 not-clean task each.
- `EG-FAILURE-MASKED` flags 1 clean task.

The two most frequent codes separate nothing:
`GT-PATH-HIJACK` fires on 94 of 95 tasks and `GT-SITE-PACKAGES-HOOK` on 84 of 95,
both at the base clean share of 52%.

## The grader-wrapper shape behind the old `WC-TINY-SUITE` result

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
The parsed suite has 1 test and 1 assertion (`assert True`).
Before HAR-509, `WC-TINY-SUITE` fired on that count.
Since HAR-509, the anatomy records `tests/grader.py` as a delegated grader module.
It does not count the module as tests, and `WC-TINY-SUITE` stays silent.

The wrapper split by executed class:

| Shape | Tasks | `clean` | `reference_below_one` | `reference_unmeasured` | `no_reference` | `cannot_run` |
|---|---:|---:|---:|---:|---:|---:|
| Grader wrapper | 37 | 2 | 6 | 3 | 26 | 0 |
| Other | 58 | 47 | 3 | 7 | 0 | 1 |

Before HAR-509, `WC-TINY-SUITE` fired on 36 of the 37 wrapper tasks, including 25 of the
26 `no_reference` tasks. Now it fires on 0 wrapper tasks.
The one remaining task is `sign-vector-game` (`clean`), a non-wrapper task
with 1 real test and 1 assertion.
The 2 clean wrapper tasks that lost the code are `anomaly-detection-ranking` and
`sales-data-csv-analysis`.

The old exception, `grpc-plant-position-server` (`no_reference`), had 6 `test_*`
functions in its `tests/grader.py`, so its suite counted 7 tests.
The anatomy no longer counts that delegated module as tests.
The task therefore also lost `WC-TEST-WITHOUT-ASSERTION`.
HAR-512 counts a raised `AssertionError` as an assertion. Two more `clean` tasks,
`service-deployment-wave-planner` and `task-xxe-exploit`, then lost `WC-TEST-WITHOUT-ASSERTION`.
The code dropped from 5 tasks to 2.
HAR-513 counts a quoted `echo 1` to a reward path variable inside a command `if` as an
exit-code gate. Two not-clean tasks, `protein-sequence` (`reference_below_one`) and
`smiles-data-lab` (`reference_unmeasured`), then gained `EG-EXIT-CODE-ONLY-GATE`.
The code went from 54 tasks (p = 1.8e-13) to 56 (p = 4.2e-12).
Every other code count is the same as in the first build.

All 26 `no_reference` tasks use the wrapper.
The old `WC-TINY-SUITE` split tracked how the import packaged these tasks, not how strong their checks are.
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
The p value sums the hypergeometric probabilities of all tables with the same margins
that are no more likely than the observed table.

| Code | Tasks | `clean` | Not clean | `reference_below_one` | `reference_unmeasured` | `no_reference` | `cannot_run` | Scores | p |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| `GT-PATH-HIJACK` | 94 | 49 | 45 | 9 | 9 | 26 | 1 | 35 | 0.48 |
| `GT-SITE-PACKAGES-HOOK` | 84 | 44 | 40 | 7 | 6 | 26 | 1 | 40 | 0.75 |
| `EG-EXIT-CODE-ONLY-GATE` | 56 | 45 | 11 | 3 | 7 | 0 | 1 | 50 | 4.2e-12 |
| `WC-INSTRUCTION-SYMBOL-UNTESTED` | 37 | 24 | 13 | 2 | 4 | 7 | 0 | 45–75 | 0.058 |
| `AR-TESTS-IN-IMAGE` | 9 | 4 | 5 | 1 | 0 | 3 | 1 | 85–92 | 0.74 |
| `EG-ARTIFACT-ONLY-CHECK` | 7 | 6 | 1 | 0 | 1 | 0 | 0 | 72 | 0.11 |
| `WC-SOURCE-TEXT-ASSERT` | 6 | 5 | 1 | 1 | 0 | 0 | 0 | 65 | 0.21 |
| `EG-INSTRUCTION-RULE-UNENFORCED` | 5 | 3 | 2 | 0 | 1 | 1 | 0 | 60 | 1 |
| `WC-NO-NEGATIVE-CASES` | 4 | 2 | 2 | 0 | 0 | 2 | 0 | 50 | 1 |
| `GT-BAKED-TESTS-EXECUTED` | 3 | 1 | 2 | 0 | 1 | 0 | 1 | 95 | 0.61 |
| `WC-NO-TESTS-FOUND` | 3 | 0 | 3 | 1 | 2 | 0 | 0 | 45 | 0.11 |
| `WC-TEST-WITHOUT-ASSERTION` | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 55 | 0.50 |
| `AR-GIT-HISTORY` | 1 | 0 | 1 | 0 | 1 | 0 | 0 | 65 | 0.48 |
| `EG-FAILURE-MASKED` | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 85 | 1 |
| `GT-AUTOLOAD-WRITABLE` | 1 | 0 | 1 | 0 | 0 | 0 | 1 | 80 | 0.48 |
| `WC-TINY-SUITE` | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 40 | 1 |

Changes against the first build: `WC-TINY-SUITE` went from 37 tasks (3 `clean`, 34 not clean,
p = 4.5e-12) to 1 task (HAR-509). `WC-TEST-WITHOUT-ASSERTION` went from 5 tasks (p = 0.36)
to 4 (HAR-509) and then to 2 (HAR-512). `EG-EXIT-CODE-ONLY-GATE` went from 54 tasks
(p = 1.8e-13) to 56 (HAR-513). No other row changed.

Notes on single tasks:

- `GT-PATH-HIJACK` misses only `okhttp-trailers-crash` (`reference_unmeasured`).
- `WC-NO-TESTS-FOUND` flags `okhttp-trailers-crash`, `protein-sequence`, and `smiles-data-lab`.
- 2 non-wrapper tasks lack `EG-EXIT-CODE-ONLY-GATE`: `jq-data-processing` and
  `service-deployment-wave-planner` (both `clean`).

## Hypotheses by executed class

| Class | Tasks | Hypotheses | Mean per task | Min | Max |
|---|---:|---:|---:|---:|---:|
| `clean` | 49 | 188 | 3.84 | 2 | 6 |
| `reference_below_one` | 9 | 24 | 2.67 | 2 | 5 |
| `reference_unmeasured` | 10 | 32 | 3.20 | 2 | 6 |
| `no_reference` | 26 | 66 | 2.54 | 2 | 4 |
| `cannot_run` | 1 | 6 | 6.00 | 6 | 6 |
| **Total** | **95** | **316** | **3.33** | **2** | **6** |

The first build had 353 hypotheses. The rebuild removes 36 `WC-TINY-SUITE` hypotheses
and 3 `WC-TEST-WITHOUT-ASSERTION` hypotheses (1 after HAR-509, 2 after HAR-512).
It adds 2 `EG-EXIT-CODE-ONLY-GATE` hypotheses (HAR-513).
Every task has at least 2 distinct codes. No task has zero hypotheses.

## Limits

- Anatomy hypotheses are descriptive, not keep/fix/discard predictions.
  This README reports counts only.
- A class comes from one executed reference and one nop cell. It measures solvability and
  nop payoff, not whether a grader accepts a wrong answer. A hypothesis about an
  exploitable grader can be true on a `clean` task.
- The separating code `EG-EXIT-CODE-ONLY-GATE` splits on the grader-wrapper shape. The shape
  is confounded with the `no_reference` class, so the split does not show that the code predicts health.
  The old `WC-TINY-SUITE` split had the same confound.
- The join measures this card snapshot, this corpus commit, and the anatomy source at the
  recorded commit. A later card, corpus, or anatomy change can change rows.
- The scan reads source bytes only. It does not build an image or run a shell file.
