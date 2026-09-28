# MiMo cloud nop qualification on Daytona (HAR-88)

**Question:** which MiMo tasks start, pass their healthcheck and grade on Daytona? The runs use the `nop` control agent: no model calls, and the expected reward is 0.

The answer feeds `broken_on_daytona`, which keeps HAR-81 baselines and the HAR-85 GEPA pool from spending model money on tasks that cannot grade in the cloud.

## Cohort (113 tasks, `cohort.json`, `make_cohort.py`)

| domain | n | selection |
|---|---|---|
| terminal | 64 | all; the grader runs 3x in place (`verifier_repeat_n: 3`, RepeatVerifier) |
| code | 32 | smallest (295 MiB, Java) and largest (13,084 MiB, JavaScript) pinned image, plus 30 seats by language share of the 2,698-task pool, largest remainder: Python 13, Go 8, JavaScript 5, TypeScript 2, Unknown 2 |
| cyber | 16 | smallest (517 MiB) and largest (13,617 MiB) image, plus one task from each of the 14 most common remaining projects |
| music | 1 | `music-gk-0000`, which failed locally (rc=127, no `/app`) |

- Within each stratum, tasks are taken in `sha256("har88:" + task_id)` order.
- Image sizes (`image-sizes.json`) are Docker Hub compressed sizes of the pinned digests, retrieved 2026-09-28. The 62 code tasks whose pinned digest is untagged on Docker Hub have no size, so they are never picked as an extreme.
- Pins are the catalog pins in `cohort.json`.
- Each spec carries `task_package_digest`, and dispatch re-verifies the staged bytes against it.

## Daytona quota: why two batches

Our org quota was read on 2026-09-28 with a read-only API call: **10 vCPU / 10 GiB memory / 30 GiB disk** (Tier 1, `us`). The batches are sized to fit it:

| batch | specs | per sandbox | fits at once | tick |
|---|---|---|---|---|
| a | terminal 64 + music 1 | 1 vCPU / 2 GiB / 10 GiB disk | 3 (disk-bound) | `--parallel 3` |
| b | code 32 + cyber 16 | 2 vCPU / 8 GiB | 1 (memory-bound) | `--parallel 1` |

- A trial that fails only because of our quota is recorded as `inconclusive (backend_quota)`, never as broken.
- Tier 2 (card linked plus a $25 top-up) raises the quota to 100 vCPU / 200 GiB / 300 GiB.

## Cost

Rates are Daytona list prices (https://www.daytona.io/pricing, retrieved 2026-09-28): $0.0504/vCPU-h, $0.0162/GiB-h memory, and $0.000108/GiB-h storage beyond 5 GiB.

**Worst case.** Every sandbox is killed by its provider TTL, where `ttl_minutes = (timeout_seconds + 600 + 59) // 60` and `timeout_seconds = build + healthcheck + verifier × repeats` from each task.toml.

| domain | n | $/sandbox-h | TTL (min) | worst case |
|---|---|---|---|---|
| terminal | 64 | 0.08334 | 72 (one task: 78) | $6.41 |
| music | 1 | 0.08280 | 70 | $0.10 |
| code | 32 | 0.23040 | 95 | $11.67 |
| cyber | 16 | 0.23040 | 65 | $3.99 |
| **total** | 113 | | | **$22.17** (batch a $6.51, batch b $15.66) |

**Expected** ≈ $2.5, assuming terminal 6 min, code 12 min, cyber 8 min and music 5 min per trial. No model tokens are used.

The measured figure replaces both numbers: sandbox-seconds × rate, per trial, from `evallab tasks qualify-collect`.

## Run (Peter; needs approval, since Daytona is a non-Docker environment)

From a clean origin/main checkout such as `~/Developer/eval-lab/.worktrees/mimo-ops`, with `DAYTONA_API_KEY` exported:

```
research/experiments/mimo-daytona-nop/stage.sh all        # pull 113 pinned tasks, submit both batches (nothing approved)
for id in $(cat derived/har88/staged-a.ids); do uv run evallab approve "$id" --actor peter; done
uv run evallab tick --parallel 3                          # batch a
for id in $(cat derived/har88/staged-b.ids); do uv run evallab approve "$id" --actor peter; done
uv run evallab tick --parallel 1                          # batch b
```

`tick` only dispatches approved specs; unapproved specs stay waiting.

## Collect

```
uv run evallab ingest runs/mimo-qual-*
uv run evallab tasks qualify-collect runs/mimo-qual-*
uv run evallab tasks catalog export-broken --backend daytona --out derived/curated/mimo-v2.6/broken-on-daytona.json
```
