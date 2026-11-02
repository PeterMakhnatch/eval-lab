> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.
> Note: Row-level file kept local; see receipts-manifest.json.

# on-label-noise-20261102: how noisy are the TraceMonitor-Bench DEV v1 labels?

Two blind readers (Sonnet 5.5 and Gemini 3.8 Flash, each with two prompt wordings) re-judged a seeded 60-case sample of the
benchmark (5 positives + 5 negatives from each of the six families) from the label-free card plus a blinded, bounded
transcript, and their answers were compared with the benchmark label per provenance class. `PREREGISTRATION.md` was
committed before any reader call. Aggregates are in `results.json`; per-case readings, prompts and sessions stay under
`~/Developer/eval-lab-results/2026-11-02/D4/` (local). No benchmark label was changed.

`label_noise.py` has `sample`, `build`, `read`, `score`; `recount.py` recomputes the consensus counts from the raw call files.
Sampling is label-balanced per family; the benchmark manifest it reads is local (`D1`, PR #1728).

## Result (60 cases, 240 calls, 238 attested, 0 fallbacks, 2 provider refusals)

| provenance class | n | consensus coverage | consensus disagreement with label | benchmark positives called honest | benchmark negatives called cheated |
|---|---:|---|---|---|---|
| container_verified | 20 | 16/20 | 1/16 = 6.2% (1-28) | 0/6 (0-39) | 1/10 (2-40) |
| publisher | 20 | 16/20 | 2/16 = 12.5% (4-36) | 2/8 (7-59) | 0/8 (0-32) |
| publisher_cot_judged | 20 | 19/20 | 9/19 = 47.4% (27-68) | 9/9 (70-100) | 0/10 (0-28) |
| all | 60 | 51/60 | 12/51 = 23.5% (14-37) | 11/23 (29-67) | 1/28 (1-18) |

Intervals are 95% Wilson, in percent.

Read this as **reader disagreement, not proven label error**. On the container-verified class (labels that are ground
truth) the readers do find the positives: consensus calls 0 of 6 honest, and reader B alone 0 of 9. On the CoT-judged class
neither reader ever said "cheated" for any of the 20 cases (0 of 20 for A, 0 of 19 answered for B), so the 9/9
positives "called honest" is the readers' blindness on that class (known: stored monitors score near chance there),
not an identified noise rate. The class estimate cannot separate label noise from reader failure.
See the report for limits (truncated transcripts, small n, refusals).

## POST-HOC sensitivity

Recall at a 1% false-alarm ceiling of a monitor that is perfect on the true label, if the consensus disagreement were all
label noise: `(1 - p) * min(1, 0.01 / q)`. Point / pessimistic / optimistic (Wilson bounds): container_verified 0.10 /
0.015 / 0.56, publisher 0.75 / 0.013 / 0.93, publisher_cot_judged 0.0 / 0.0 / 0.30, all 0.145 / 0.019 / 0.71. These are
upper-bound disagreement readings, not noise estimates, and nothing was relabelled.
