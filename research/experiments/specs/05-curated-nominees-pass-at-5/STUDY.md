# Study 05 — Codex pass@5 on the five canary-nominated curated tasks

**Hypothesis.** Codex pass@5 on CURATOR's five canary nominees is a
broader baseline than the three already-pinned local canaries.

**One variable.** Task identity among:

| Priority | Task | Domain | Oracle k=3 (CURATOR card) |
|---|---|---|---|
| 1 | html-js-filter | Security/AppSec | 1.0 |
| 2 | foodstuff-beta-activity | Science/Chemistry | 1.0 |
| 3 | fin-saccr-rwa | Operations/Finance | 1.0 |
| 4 | interleaved-vigenere | Security/Cryptography | 1.0 |
| 5 | bun-sourcemap-leak | Software/Systems | 1.0 |

**Fixed.** `agent=codex`, `attempts=5`, docker.

**Why this is not runnable tonight.** Four independent blockers, none of
which RUNNER may remove:

1. `library/curated/<name>/` holds a `CARD.md` only. There is no `task.toml`
   in this checkout. CURATOR's cards point at
   `~/Developer/agent-evals/frontier-bench` @ `3d694e91`, which is outside
   this repository.
2. Standing policy does not match `library/curated/*`. Using `canary/`
   would invent suite members. Using `registered/*` is a scope question
   for Peter.
3. `attempts=5` exceeds `canary` max_attempts=3.
4. At the suite's $2.50 / 3-attempt rate, five attempts cost $4.17 and
   hit `per_job_cost_ceiling`.

The submitted representative uses `attempts=3` and `est_cost_usd=2.50`
so the recorded reason code is `out_of_policy` (namespace), not a cost
ceiling that hides the registration question. The other four nominees are
the same refusal; they are listed here and not separately queued.

**What CURATOR already measured.** Each nominee has oracle k=3 = 1.0 and
nop = 0.0 in `~/Developer/helab-curator/runs/`. That is task-validity
evidence, not a Codex result.

**Next spec this implies.** Peter registers a 5-task `registered/*` slice
(or promotes the nominees into `policy/canary-suite.yaml` with digests)
and either accepts n=3 or raises the per-job ceiling / measures a cheaper
per-attempt cost. Do not copy frontier-bench trees into this repo from
RUNNER.

## 2026-08-15 PROGRAM reconciliation

Still no Codex jobs on curated cards in primary `runs/`. Status remains
waiting / `out_of_policy`. Do not use 2026-08-15 local html-js-filter
canary results as a curated-nominee result (different path / pin).
