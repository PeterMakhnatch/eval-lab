table: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har108-census-20260930/research/experiments/har108-python-census/task_health.parquet
rows: 1180

# Task health census

## By label

| label | tasks |
|---|---:|
| sound | 493 |
| broken_environment | 59 |
| grader_suspect | 2 |
| unknown | 626 |

## By split

| split | sound | broken_environment | grader_suspect | unknown |
|---|---:|---:|---:|---:|
| heldout | 57 | 6 | 1 | 69 |
| train | 436 | 53 | 1 | 557 |

## By project

projects: 935
singleton projects: 812

Top 25 by task count (group by project_key; ties broken by project_key):

| project_key | tasks | sound | broken_environment | grader_suspect | unknown |
|---|---:|---:|---:|---:|---:|
| pandas | 27 | 0 | 10 | 0 | 17 |
| torch | 16 | 5 | 0 | 0 | 11 |
| github.com/apache/airflow | 13 | 7 | 0 | 0 | 6 |
| homeassistant | 9 | 3 | 1 | 0 | 5 |
| github.com/sqlfluff/sqlfluff | 8 | 4 | 0 | 0 | 4 |
| Lib | 5 | 3 | 0 | 0 | 2 |
| dbt | 5 | 2 | 0 | 0 | 3 |
| github.com/pandas-dev/pandas | 5 | 0 | 3 | 0 | 2 |
| httpx | 5 | 2 | 0 | 0 | 3 |
| scipy | 5 | 2 | 0 | 0 | 3 |
| socorro | 5 | 1 | 2 | 0 | 2 |
| dask | 4 | 1 | 1 | 0 | 2 |
| github.com/PyTorchLightning/pytorch-lightning | 4 | 2 | 0 | 0 | 2 |
| github.com/user-attachments/assets | 4 | 2 | 1 | 0 | 1 |
| google | 4 | 3 | 0 | 0 | 1 |
| numpy | 4 | 2 | 1 | 0 | 1 |
| sqlfluff | 4 | 3 | 0 | 0 | 1 |
| sunpy | 4 | 3 | 0 | 0 | 1 |
| alpaca | 3 | 0 | 0 | 0 | 3 |
| celery | 3 | 1 | 0 | 0 | 2 |
| cirq | 3 | 3 | 0 | 0 | 0 |
| common | 3 | 1 | 0 | 0 | 2 |
| gammapy | 3 | 0 | 0 | 0 | 3 |
| geopandas | 3 | 2 | 0 | 0 | 1 |
| github.com/home-assistant/core | 3 | 2 | 0 | 0 | 1 |

## Projects with 2 or more broken tasks

projects with >=2 broken tasks: 3

| project_key | broken_environment |
|---|---:|
| pandas | 10 |
| github.com/pandas-dev/pandas | 3 |
| socorro | 2 |

## Top reasons

One count per task per entry of the reasons list.

| reason | tasks |
|---|---:|
| no_nop | 626 |
| setup_error | 57 |
| tests_did_not_run | 2 |
| environment_exception | 2 |
| tests_not_applied | 2 |
| reward_missing | 2 |

## Leak

Group by leak_channel.

| leak_channel | tasks |
|---|---:|
| pypi_fix_released | 247 |
| pypi_package | 367 |
| git_only | 72 |
| none_found | 494 |
| unknown | 0 |

Cross-tab of leak_channel and label.

| leak_channel | sound | broken_environment | grader_suspect | unknown |
|---|---:|---:|---:|---:|
| pypi_fix_released | 97 | 23 | 0 | 127 |
| pypi_package | 162 | 21 | 1 | 183 |
| git_only | 32 | 1 | 0 | 39 |
| none_found | 202 | 14 | 1 | 277 |
| unknown | 0 | 0 | 0 | 0 |

Group by leak_pypi_match. Null is (none).

| leak_pypi_match | tasks |
|---|---:|
| name | 430 |
| repo_url | 193 |
| (none) | 557 |

## Nop cost

Sum of nop_cost_usd; null counts as 0.

Total nop cost: $3.1204.
