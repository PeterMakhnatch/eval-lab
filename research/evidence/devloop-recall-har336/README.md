# Devloop selection recall

Run date (UTC): 2026-11-09T14:57:07.481090+00:00

## Method

Replay the most recent failed `quality` PR runs, in descending creation order.
Extract unique failing Python files from pytest `FAILED` and `ERROR` log lines.
Compare both planners on each run's archived source, test roots, and configuration.
Changed files use the merge base between the run head and the recorded `origin/main`.
Before: `origin/main` (`ac529b697a2a739fa1022d24fb20f744ea57087d`).
After: working-tree `src/evallab/devloop.py`.
No historical tests run. A full-suite fallback counts every failing file as a hit.
Selected counts expand a fallback to the archived Python test-consumer inventory.

Usable runs: 30; attempts: 39; skipped: 9.

## Aggregate metrics

File recall = total hits / total failing-file observations (weighted across runs).
Mean per-run recall = the mean of each run's hits / failing files (equal run weights).
Fallback rate = full-suite fallback runs / usable runs.

| Metric | Before | After |
|---|---:|---:|
| File recall | 100.0% | 100.0% |
| Mean per-run recall | 100.0% | 100.0% |
| Fallback rate | 80.0% | 33.3% |
| Median selected file count | 800.5 | 285.0 |
| Hits | 39 | 39 |
| Misses | 0 | 0 |

## Per-run results

| Run | Branch | Failing files | Hits before / after | Recall before / after | Fallback before / after |
|---|---|---|---:|---:|---|
| 42570550258 | foreman/har-330-quality-has-been-failing-on-main-since-8 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | True / True |
| 42105554180 | ev1105-w3 | `tests/test_admission.py`, `tests/test_ci_coverage.py` | 2 / 2 | 100.0% / 100.0% | True / False |
| 42104270721 | ev1105-w3 | `tests/test_admission.py`, `tests/test_ci_coverage.py` | 2 / 2 | 100.0% / 100.0% | True / False |
| 41917612865 | wf-w1 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41915244773 | wf-w1 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41914213520 | wf-w6 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | False / False |
| 41913830875 | ov1106-lengthctl | `research/experiments/ov1106-lengthctl/test_lengthctl_scoreboard.py` | 1 / 1 | 100.0% / 100.0% | True / True |
| 41912315234 | wf-w2 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | True / True |
| 41912247523 | ov1106-lengthctl | `research/experiments/ov1106-lengthctl/test_scoreboard.py` | 1 / 1 | 100.0% / 100.0% | True / True |
| 41763507722 | foreman/har-306-bounded-daytona-environment-tests-cover | `tests/test_ci_coverage.py`, `tests/test_harbor_daytona.py` | 2 / 2 | 100.0% / 100.0% | True / True |
| 41762331326 | foreman/har-306-bounded-daytona-environment-tests-cover | `tests/test_ci_coverage.py` | 1 / 1 | 100.0% / 100.0% | True / True |
| 41708467335 | foreman/har-293-format-code-task-002661-and-001671-each | `research/experiments/mimo-clean-v6/test_ladder_progress.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41708199317 | foreman/har-293-format-code-task-002661-and-001671-each | `research/experiments/mimo-clean-v6/test_ladder_progress.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41700695172 | foreman/har-297-ci-test-jobs-start-a-postgres-18-4-servi | `dashboard/tests/test_queries.py`, `tests/test_attach.py`, `tests/test_verdicts.py` | 3 / 3 | 100.0% / 100.0% | True / True |
| 41700379826 | foreman/har-297-ci-test-jobs-start-a-postgres-18-4-servi | `dashboard/tests/test_queries.py`, `tests/test_attach.py`, `tests/test_verdicts.py` | 3 / 3 | 100.0% / 100.0% | True / True |
| 41621621645 | fix/goodfire-hackdrop-nested-tools | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | False / False |
| 41566200542 | nov2-line-residuals | `tests/test_judge_station.py` | 1 / 1 | 100.0% / 100.0% | False / False |
| 41564902073 | nov2-live-line | `tests/test_contextpack.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41558853799 | fix/goodfire-hackdrop-tool-shape | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | False / False |
| 41556190017 | nov2-independent-review | `tests/test_contextpack.py` | 1 / 1 | 100.0% / 100.0% | True / True |
| 41555658386 | insights/analyst-engine-20261102 | `tests/test_contextpack.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41553263810 | insights/analyst-engine-20261102 | `tests/test_contextpack.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41552847416 | insights/analyst-engine-20261102 | `tests/test_contextpack.py`, `tests/test_docindex.py` | 2 / 2 | 100.0% / 100.0% | True / False |
| 41548394165 | nov2-line-core | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | True / True |
| 41441900868 | on/l0-login-profile-rule-20261102 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41423926848 | foreman/har-255-sheet-recalc-and-sheet-recalc-cutoff-eac | `research/experiments/tests/test_library_ladder_coverage.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41409437025 | on/r6-ledger-reconcile-20261102 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | False / False |
| 41274629934 | ov/w2-autoreview-investigator-20261031 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | True / False |
| 41274537910 | ov-k3-nonpytest-20261031 | `tests/test_ci_coverage.py`, `tests/test_nonpytest_hardening.py` | 2 / 2 | 100.0% / 100.0% | True / False |
| 41272608736 | ov/t1-build-anatomy-20261031 | `tests/test_docindex.py` | 1 / 1 | 100.0% / 100.0% | False / False |

## Remaining misses

No post-change misses occur in the usable runs.

## Skipped runs

- Run 41700445908: no pytest FAILED/ERROR lines. 
- Run 41690512979: no pytest FAILED/ERROR lines. 
- Run 41677372121: no pytest FAILED/ERROR lines. 
- Run 41565527024: no pytest FAILED/ERROR lines. 
- Run 41554933970: no pytest FAILED/ERROR lines. 
- Run 41544013674: no pytest FAILED/ERROR lines. 
- Run 41419051150: no pytest FAILED/ERROR lines. 
- Run 41401947614: no pytest FAILED/ERROR lines. 
- Run 41277938313: no pytest FAILED/ERROR lines. 

## Limits

- Failed-run sampling is not a random sample of all PRs. Repeat heads remain separate runs.
- The denominator counts each unique failing file once per run, not each failing test case.
- Logs without pytest FAILED/ERROR lines, unavailable heads, and invalid trees are skipped.
- Recall measures file selection only. It does not establish test success or root cause.
- Full-suite fallback can raise recall while selecting more files; inspect its rate and size.
- Fallback size counts Python consumers under test roots, excluding conftest.py. It is not pytest collection.
- Static import and string matching cannot prove dynamic runtime dependencies.
- The current origin/main merge base can differ from the original CI merge base.
- The working-tree after planner can contain uncommitted edits; preserve it with the evidence.
- A bounded smoke run does not satisfy the default 30-run measurement requirement.
