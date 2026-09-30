table: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har113-variants-20260930/research/experiments/har108-python-census/task_health.parquet
rows: 1180

# Task health census

## By label

| label | tasks |
|---|---:|
| sound | 870 |
| broken_environment | 107 |
| grader_suspect | 5 |
| unknown | 198 |

## By split

| split | sound | broken_environment | grader_suspect | unknown |
|---|---:|---:|---:|---:|
| heldout | 104 | 9 | 1 | 19 |
| train | 766 | 98 | 4 | 179 |

## By project

projects: 935
singleton projects: 812

Top 25 by task count (group by project_key; ties broken by project_key):

| project_key | tasks | sound | broken_environment | grader_suspect | unknown |
|---|---:|---:|---:|---:|---:|
| pandas | 27 | 0 | 25 | 0 | 2 |
| torch | 16 | 11 | 0 | 0 | 5 |
| github.com/apache/airflow | 13 | 12 | 0 | 0 | 1 |
| homeassistant | 9 | 5 | 1 | 0 | 3 |
| github.com/sqlfluff/sqlfluff | 8 | 8 | 0 | 0 | 0 |
| Lib | 5 | 3 | 0 | 0 | 2 |
| dbt | 5 | 4 | 0 | 0 | 1 |
| github.com/pandas-dev/pandas | 5 | 0 | 5 | 0 | 0 |
| httpx | 5 | 4 | 1 | 0 | 0 |
| scipy | 5 | 2 | 0 | 0 | 3 |
| socorro | 5 | 1 | 2 | 0 | 2 |
| dask | 4 | 3 | 1 | 0 | 0 |
| github.com/PyTorchLightning/pytorch-lightning | 4 | 4 | 0 | 0 | 0 |
| github.com/user-attachments/assets | 4 | 3 | 1 | 0 | 0 |
| google | 4 | 4 | 0 | 0 | 0 |
| numpy | 4 | 2 | 1 | 0 | 1 |
| sqlfluff | 4 | 4 | 0 | 0 | 0 |
| sunpy | 4 | 3 | 0 | 0 | 1 |
| alpaca | 3 | 3 | 0 | 0 | 0 |
| celery | 3 | 3 | 0 | 0 | 0 |
| cirq | 3 | 3 | 0 | 0 | 0 |
| common | 3 | 1 | 0 | 0 | 2 |
| gammapy | 3 | 2 | 0 | 0 | 1 |
| geopandas | 3 | 3 | 0 | 0 | 0 |
| github.com/home-assistant/core | 3 | 3 | 0 | 0 | 0 |

## Projects with 2 or more broken tasks

projects with >=2 broken tasks: 3

| project_key | broken_environment |
|---|---:|
| pandas | 25 |
| github.com/pandas-dev/pandas | 5 |
| socorro | 2 |

## Top reasons

One count per task per entry of the reasons list.

| reason | tasks |
|---|---:|
| no_nop | 198 |
| setup_error | 104 |
| tests_did_not_run | 5 |
| environment_exception | 3 |
| tests_not_applied | 3 |
| reward_missing | 3 |

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
| pypi_fix_released | 187 | 56 | 1 | 3 |
| pypi_package | 277 | 26 | 1 | 63 |
| git_only | 66 | 2 | 0 | 4 |
| none_found | 340 | 23 | 3 | 128 |
| unknown | 0 | 0 | 0 | 0 |

Group by leak_pypi_match. Null is (none).

| leak_pypi_match | tasks |
|---|---:|
| name | 430 |
| repo_url | 193 |
| (none) | 557 |

## Nop cost

Sum of nop_cost_usd; null counts as 0.

Total nop cost: $4.8331.
