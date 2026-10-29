# process-job wall time per trial (HAR-223), 2026-10-29

**Verdict: no change to `process_job.py`.** Trial-level parallelism is not
added: the serial per-trial loop is not where `evallab process-job` spends its
time on retained jobs, and trial-level fan-out has nothing to split on them.

## What was measured

29 retained jobs, 5 repeats each, two call shapes, on read-only copies in
`/private/tmp` (nothing processed in place; `eval-lab-results` never written):

- **post-run** = how the runner calls it: `process_job(job, root=repo)` with
  `ingest=False` and publish, into a mirror of the results home that holds the
  8,200 published jobs' INDEX inputs, so the INDEX rewrite sees today's job count.
- **nightly** = how `nightly_refresh` calls it: `ingest=False, publish=False`
  plus the Parquet projection (`parquet_root`).

Jobs: 25 single-trial jobs from `~/Developer/eval-lab-results`, stratified by
size (random 600 of the single-trial jobs, seed 223, then size quantiles
0 to 1: 0.04 MB to 355 MB), plus the 4 largest multi-trial jobs found
anywhere (3-trial and 12-trial; see "Trial-count census"). Rows are the repeat
with the lowest wall time; median and max over repeats are shown because the
Mac was busy (load average 13 to 22): the same INDEX rewrite that takes 1.2 s
here took up to 23 s in an earlier, contended pass.

Reproduce: `uv run python research/explorations/process-job-timing-2026-10-29/measure.py --jobs-file research/explorations/process-job-timing-2026-10-29/jobs.txt --out /tmp/t.csv`.
Full per-phase columns for both modes: [process-job-timing.csv](process-job-timing.csv).
`MB` is the staged copy (published tree minus `provenance.json`/`uncommitted.diff`, plus the regenerated `processed/`).

## Timing table (seconds)

"trial loop" = the `for trial_path in trials` body in `process_job` (`_process_trial`,
counts, decision, estimate). "nightly per-trial share" = (trial loop + Parquet
projection) / wall, i.e. all work that scales with trial count.

| Job | Trials | MB | post-run best (median) s | trial loop s | INDEX rewrite s | publish copy s | nightly best s | nightly projection s | nightly per-trial share |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| har115-rnop-002451-78f8541d0e9d | 1 | 0.04 | 1.19 (1.24) | 0.002 | 1.18 | 0.003 | 0.011 | 0.009 | 89% |
| har113-nop-001618 | 1 | 0.06 | 1.25 (1.27) | 0.002 | 1.24 | 0.003 | 0.011 | 0.009 | 89% |
| har140-lnop-002450 | 1 | 0.06 | 1.23 (1.24) | 0.002 | 1.22 | 0.004 | 0.011 | 0.009 | 89% |
| har146-lnop-002878 | 1 | 0.08 | 1.24 (1.35) | 0.002 | 1.23 | 0.003 | 0.013 | 0.010 | 86% |
| hack-00-oracle-swebench-verified-pylin-fbcdc | 1 | 0.08 | 1.27 (1.32) | 0.002 | 1.25 | 0.008 | 0.014 | 0.011 | 90% |
| ec-zxekad-m-66cf836400-session-window-debug | 1 | 0.09 | 1.25 (1.30) | 0.002 | 1.23 | 0.008 | 0.014 | 0.011 | 92% |
| bbc-020-nop-mars-clouds-clustering | 1 | 0.11 | 1.19 (1.21) | 0.002 | 1.18 | 0.005 | 0.012 | 0.009 | 89% |
| har146-lnop-000181-r4 | 1 | 0.15 | 1.21 (1.23) | 0.002 | 1.20 | 0.004 | 0.012 | 0.009 | 86% |
| cb-pf-10-q36or-b2-on | 1 | 0.25 | 1.28 (1.33) | 0.009 | 1.26 | 0.006 | 0.053 | 0.045 | 97% |
| qa-replay-01m54e0wygs29dbkpr5xcy83z0 | 1 | 0.57 | 1.21 (1.23) | 0.002 | 1.20 | 0.007 | 0.014 | 0.011 | 91% |
| mimo-train40-001695-oracle-source-20261018-b | 1 | 0.98 | 1.20 (1.22) | 0.002 | 1.17 | 0.022 | 0.018 | 0.015 | 92% |
| har161r-exploit-000792 | 1 | 2.03 | 1.22 (1.24) | 0.020 | 1.19 | 0.006 | 0.087 | 0.065 | 98% |
| bbh-terminus-t2t-build-pmars-dbc6-bl-a1 | 1 | 2.76 | 1.61 (1.71) | 0.006 | 1.59 | 0.007 | 0.031 | 0.023 | 95% |
| vr-gzaa4g1w-survivor-673042f923 | 1 | 3.50 | 1.20 (1.26) | 0.002 | 1.18 | 0.011 | 0.017 | 0.014 | 91% |
| heldout-control-nop-format-code-task-002092 | 1 | 12.75 | 1.40 (1.48) | 0.002 | 1.38 | 0.009 | 0.018 | 0.015 | 93% |
| hack-mmp6k6-02-02-bbh-terminus-make-mips-in- | 1 | 15.53 | 1.20 (1.24) | 0.002 | 1.18 | 0.006 | 0.020 | 0.017 | 93% |
| ec-mra0za-b-704f6a7f21-format-code-task-0024 | 1 | 20.20 | 1.20 (1.29) | 0.002 | 1.19 | 0.008 | 0.023 | 0.020 | 93% |
| qa-replay-01m56nd310d6773p3dx0pd57wq | 1 | 53.64 | 1.23 (1.30) | 0.002 | 1.20 | 0.015 | 0.035 | 0.032 | 96% |
| har146-lnop-000163 | 1 | 71.49 | 1.26 (1.29) | 0.070 | 1.17 | 0.013 | 0.543 | 0.472 | 100% |
| ec-76b5nb-m-ad055faf85-format-code-task-0022 | 1 | 87.65 | 1.20 (1.27) | 0.002 | 1.18 | 0.018 | 0.048 | 0.045 | 97% |
| qa-replay-01m56t3jw0de8dz4e2z0rhvvk2 | 1 | 119.21 | 1.23 (1.24) | 0.002 | 1.19 | 0.024 | 0.060 | 0.058 | 98% |
| ec-yd6eta-m-96c05b3e8e-format-code-task-0005 | 1 | 144.04 | 1.27 (1.30) | 0.002 | 1.23 | 0.026 | 0.071 | 0.068 | 98% |
| sepsv3-tamper-001172 | 1 | 181.49 | 1.31 (1.38) | 0.002 | 1.27 | 0.029 | 0.091 | 0.086 | 98% |
| har146-lnop-000125-r2 | 1 | 211.31 | 1.68 (1.70) | 0.373 | 1.26 | 0.030 | 1.832 | 1.592 | 100% |
| har146-lnop-002991-r5 | 1 | 355.51 | 1.61 (1.63) | 0.328 | 1.23 | 0.047 | 4.074 | 3.743 | 100% |
| canary-transaction-reconciliation-codex-2026 | 3 | 0.21 | 1.26 (1.30) | 0.009 | 1.23 | 0.009 | 0.075 | 0.063 | 96% |
| canary-event-summary-codex-20260816 | 3 | 0.30 | 1.28 (1.29) | 0.012 | 1.24 | 0.011 | 0.094 | 0.078 | 97% |
| har83-qwen-terminal-0036 | 3 | 2.99 | 1.33 (1.41) | 0.027 | 1.27 | 0.021 | 0.157 | 0.127 | 98% |
| har168-native-selected12-6da15fc1d06c | 12 | 74.73 | 1.57 (1.67) | 0.258 | 1.21 | 0.071 | 1.688 | 1.416 | 99% |

## What the data says

1. **Trial-count census.** `~/Developer/eval-lab-results` has 8,380 job
   directories under `<date>/`; 164 are not jobs (no `result.json`) and every
   one of the other 8,213 well-formed jobs has exactly one trial (job-level
   `n_total_trials == 1` for 8,211; the other 5 record none, 3 of them the
   containers below). Three directories
   look multi-trial (`admit-oracle-fix`, `repeat-oracle-fix`,
   `mimoagent-clean-smoke`) but are containers of several jobs, not jobs;
   `process_job` finds 0 trials in them. Across the primary checkout's `runs/`
   and every worktree's `runs/` (about 2,400 jobs) the largest job has 12
   trials and almost all have 1. Fan-out over trials has nothing to split on for
   the jobs that are published.
2. **Post-run (the publish path): the trial loop is 0.1% of wall at the median
   (max 22%, on the two heaviest single trials).** 98% of wall (75% to 99%) is
   `publish_job`'s `write_index`: it re-reads `provenance.json`, `config.json`,
   `lab-metadata.json` and `processed/job.json` for every published job on
   each publish, 1.17 to 1.59 s at 8.2k jobs regardless of the job being
   published. It grows with the home, not the job: a separate scaling run
   gave 0.10 s at 1k, 0.22 s at 2k, 0.52 s at 4k, 1.12 s at 8.2k jobs
   (best of 3). The byte copy of the job is 0.003 to 0.07 s. Under
   contention the same step ran 12 to 23 s.
3. **Nightly (projection on): per-trial work is 86% to 100% of wall, but wall
   is tiny.** Median 31 ms per job; only three jobs exceed 1 s (1.7 to 4.1 s),
   and two of those (1.8 s and 4.1 s) are single-trial jobs dominated by
   `trial_features` on one large trial, which fan-out over trials cannot split. The 12-trial job
   costs 0.14 s per trial (1.69 s total); fan-out could save at most about
   1.4 s on it, and no other retained job has more than 3 trials.
4. **Not a loop cost, but visible:** a fresh interpreter pays about 2 s of
   lazy imports on the first call (nightly shape, tiny job: 2.0 to 2.7 s
   first call vs 0.011 s warm; post-run shape 3.7 to 8.5 s vs 1.25 s warm,
   at load 13). The nightly service and the runner run `process_job` in
   process, so this is paid once per process, not per trial. The standalone
   CLI pays it per job.
5. **Ingest** (post-run default, throwaway scratch Postgres, tiny catalog):
   `initialize` + `ingest` + `load_job` was 20 to 160 ms (grows with job size).
   Not measured against the shared catalog; I did not write to it.

## Why no parallelism

The card's gate was "parallelize only if the serial loop dominates at
realistic trial counts". Realistic here is 1 trial; at 1 trial the loop is
under 1% of publish and there is no second trial to run concurrently. Adding a
worker pool would add ordering and shared-state risk and a process-start cost
(item 4) to every call for no measured gain, so none was added and no
byte-identity comparison was needed.

## What would change the answer

- Jobs with many trials becoming routine. Break-even for the publish path:
  the loop reaches half of wall at about (1.2 s INDEX) / (per-trial cost)
  trials: ~600 trials at the typical 2 ms per trial, but ~4 trials at the
  0.3 s of a very large trajectory (0.33 to 0.37 s measured). For the nightly
  shape the projection is already ~99% per-trial work, so a 100-trial job at
  0.14 s per trial would cost about 14 s serial and would justify a bounded
  pool around `_project_job`, not around the loop in `process_job.py`.
- INDEX cost growing: it scales with the number of published jobs
  (about 0.14 ms per job). The right fix for slow publishing is an
  incremental INDEX update in `results_home.write_index`, not trial
  parallelism; that is a separate card and was not attempted here.
- Re-run `measure.py` with a jobs file that includes new multi-trial jobs when
  they exist.

## Limits

- Wall-clock on a shared, loaded Mac; best-of-5 is reported with median and
  max. Page-cache state was warm after the first repeat.
- INDEX inputs were mirrored from the real results home (files `write_index`
  reads), not read from it in place; the mirror held 8,200 published jobs
  plus the measured one.
- Catalog ingest and fresh-process numbers (items 4 and 5) came from throwaway
  harnesses, not `measure.py`; the table and CSV come from `measure.py`.
- Multi-trial evidence is thin by construction: there are no multi-trial jobs
  in `eval-lab-results`; the supplement jobs came from `runs/` and a worktree
  (the worktree path may be gone later; the numbers stay in the CSV).
