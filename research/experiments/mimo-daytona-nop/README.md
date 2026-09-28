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
| b | code 32 + cyber 16 | 2 vCPU / 8 GiB / 10 GiB disk | 1 (memory-bound) | `--parallel 1` |

- A trial that fails only because of our quota is recorded as `inconclusive (backend_quota)`, never as broken.
- Tier 2 (card linked plus a $25 top-up) raises the quota to 100 vCPU / 200 GiB / 300 GiB.

## Disk: the per-sandbox cap (`unpacked-sizes.json`)

Whatever the org tier, Daytona caps each sandbox at **4 vCPU / 8 GiB RAM / 10 GiB disk**, and a sandbox that doesn't request disk gets **3 GiB** (https://www.daytona.io/docs/en/sandboxes/).
- Terminal tasks declare `storage_mb = 10240`.
- Code, cyber and music tasks declare no storage. Harbor then sends no disk request (`harbor/environments/daytona/environment.py`), so they would get the 3 GiB default.

Unpacked image sizes were measured for every code and cyber image in the cohort. Method: the gzip ISIZE trailer of each layer, fetched with an HTTP Range request, cross-checked against `du -sxm /` on three locally pulled images.
- **3 GiB default:** only 12 of the 32 code images fit.
- **10 GiB maximum:** 26 code images fit with ≥ 1 GiB headroom; 5 are marginal (8.6–9.5 GiB, or above 10 GiB if the 4 GiB ISIZE wrap resolves upward); the largest (`format-code-task-000647`, ≥ 26 GiB) cannot fit on Daytona at all.
- **Cyber:** 15 of 16 fit; the largest (`arvo_32142`, ≥ 29 GiB) cannot fit.
- **Whole pools** (compressed size × ~2.7): about 2,095 of 2,636 sized code tasks fit 10 GiB, 257 are marginal and 284 exceed it; 997 of 1,000 cyber tasks fit.

So every spec whose task leaves `storage_mb` unset carries `override_storage_mb: 10240` (Harbor `--override-storage-mb`).

A setup failure on disk capacity is recorded as `inconclusive (backend_quota)`, not broken: the task may run on a larger backend.

## Cost

Rates are Daytona list prices (https://www.daytona.io/pricing, retrieved 2026-09-28): $0.0504/vCPU-h, $0.0162/GiB-h memory, and $0.000108/GiB-h storage beyond 5 GiB.

**Worst case.** Every sandbox is killed by its provider TTL, where `ttl_minutes = (timeout_seconds + 600 + 59) // 60` and `timeout_seconds = build + healthcheck + verifier × repeats + 300 s`, taken from each task.toml. `timeout_seconds` is also the executor's per-trial watchdog; the 300 s margin keeps it from killing a trial that uses its full phase budgets.

Every sandbox bills 10 GiB of disk: terminal tasks declare it, and the other domains get it through `override_storage_mb`.

| domain | n | $/sandbox-h | TTL (min) | worst case |
|---|---|---|---|---|
| terminal | 64 | 0.08334 | 77 (one task: 83) | $6.85 |
| music | 1 | 0.08334 | 75 | $0.10 |
| code | 32 | 0.23094 | 100 | $12.32 |
| cyber | 16 | 0.23094 | 70 | $4.31 |
| **total** | 113 | | | **$23.59** (batch a $6.96, batch b $16.63) |

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
