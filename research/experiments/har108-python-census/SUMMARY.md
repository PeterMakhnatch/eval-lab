table: /Users/petermakhnatch/Developer/eval-lab/.worktrees/har115-census-20261001/research/experiments/har108-python-census/task_health.parquet
rows: 1180

# Task health census

## By label

| label | tasks |
|---|---:|
| sound | 1040 |
| broken_environment | 135 |
| grader_suspect | 5 |
| unknown | 0 |

## By split

| split | sound | broken_environment | grader_suspect | unknown |
|---|---:|---:|---:|---:|
| heldout | 121 | 12 | 0 | 0 |
| train | 919 | 123 | 5 | 0 |

## By project

projects: 935
singleton projects: 812

Top 25 by task count (group by project_key; ties broken by project_key):

| project_key | tasks | sound | broken_environment | grader_suspect | unknown |
|---|---:|---:|---:|---:|---:|
| pandas | 27 | 2 | 25 | 0 | 0 |
| torch | 16 | 16 | 0 | 0 | 0 |
| github.com/apache/airflow | 13 | 13 | 0 | 0 | 0 |
| homeassistant | 9 | 8 | 1 | 0 | 0 |
| github.com/sqlfluff/sqlfluff | 8 | 8 | 0 | 0 | 0 |
| Lib | 5 | 4 | 1 | 0 | 0 |
| dbt | 5 | 5 | 0 | 0 | 0 |
| github.com/pandas-dev/pandas | 5 | 0 | 5 | 0 | 0 |
| httpx | 5 | 4 | 1 | 0 | 0 |
| scipy | 5 | 5 | 0 | 0 | 0 |
| socorro | 5 | 2 | 3 | 0 | 0 |
| dask | 4 | 3 | 1 | 0 | 0 |
| github.com/PyTorchLightning/pytorch-lightning | 4 | 4 | 0 | 0 | 0 |
| github.com/user-attachments/assets | 4 | 3 | 1 | 0 | 0 |
| google | 4 | 4 | 0 | 0 | 0 |
| numpy | 4 | 3 | 1 | 0 | 0 |
| sqlfluff | 4 | 4 | 0 | 0 | 0 |
| sunpy | 4 | 3 | 1 | 0 | 0 |
| alpaca | 3 | 3 | 0 | 0 | 0 |
| celery | 3 | 3 | 0 | 0 | 0 |
| cirq | 3 | 3 | 0 | 0 | 0 |
| common | 3 | 3 | 0 | 0 | 0 |
| gammapy | 3 | 2 | 1 | 0 | 0 |
| geopandas | 3 | 3 | 0 | 0 | 0 |
| github.com/home-assistant/core | 3 | 3 | 0 | 0 | 0 |

## Projects with 2 or more broken tasks

projects with >=2 broken tasks: 7

| project_key | broken_environment |
|---|---:|
| pandas | 25 |
| github.com/pandas-dev/pandas | 5 |
| socorro | 3 |
| ape | 2 |
| arches | 2 |
| camelot | 2 |
| github.com/napari/napari | 2 |

## Top reasons

One count per task per entry of the reasons list.

| reason | tasks |
|---|---:|
| setup_error | 130 |
| tests_did_not_run | 5 |
| environment_exception | 5 |
| tests_not_applied | 5 |
| reward_missing | 5 |

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
| pypi_fix_released | 190 | 56 | 1 | 0 |
| pypi_package | 331 | 35 | 1 | 0 |
| git_only | 69 | 3 | 0 | 0 |
| none_found | 450 | 41 | 3 | 0 |
| unknown | 0 | 0 | 0 | 0 |

Group by leak_pypi_match. Null is (none).

| leak_pypi_match | tasks |
|---|---:|
| name | 430 |
| repo_url | 193 |
| (none) | 557 |

## Nop cost

Sum of nop_cost_usd; null counts as 0.

Total nop cost: $6.5014.
