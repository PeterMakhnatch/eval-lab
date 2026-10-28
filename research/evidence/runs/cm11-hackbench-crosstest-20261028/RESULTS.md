# CM-11 results: HackBench cross-test (aggregate counts only)

Frozen design: `README.md` and `results/freeze.json` (commit `511e0cd35`; `window-cascade/v7`, production defaults,
Stage-2 prompt `2b8a61cb…`). Primary judge `kimi-code/k3.1-pro-highspeed` (176 attested calls, 0 failed, 0
trajectories excluded); sensitivity judge `meta/muse-wonder-1.0-contributor`. Nothing was tuned on HackBench.
No dataset text, judge reply or quote is stored here; per-trajectory records (hashes and indices) stay in `~/.local/state/evallab-cross-test/meta-hackbench/meta-hackbench-cm11/`
(the README's `meta-hackbench-cm11/` path resolves under `meta-hackbench/`).

## Command localization, 169 trajectories (Kimi, `command` layout; gold density = chance precision 4.0%)

| Arm | Alarmed | Exact-command hit | Hit within 5 / 20 | Mean P / R / F1 | Alarm before first gold |
|---|---:|---:|---:|---|---:|
| Stage 1 (any card) | 105 | 9 (5.3%) [2.8, 9.8] | 14 / 21 | 0.033 / 0.036 / 0.024 | 94 (55.6%) |
| Cascade (confirmed + pass-through) | 7 | 2 (1.2%) [0.3, 4.2] | 2 / 3 | 0.009 / 0.009 / 0.009 | 6 (3.6%) |

Natural (41): Stage 1 4 exact hits, cascade 1 [0.4, 12.6]. Synthetic (128): Stage 1 5, cascade 1. Task-cluster
bootstrap 95% for cascade recall 0.0 to 0.018. Turn layout (production shape, scored per turn): cascade
6 alarmed, 2 exact, recall 0.012. Muse judge: cascade 4 alarmed, 1 exact. No judge can exceed the Stage-1
ceiling (9 exact hits); no prefilter family fired on any HackBench command, and `submission_tamper` and
`grader_tamper` raised no pass-through card. Budget was not the cause: 2 trajectories spent three judged
windows before the first gold command, and no gold command window was clipped (command layout).

## Facets and union (coverage-limited)

Run Atlas facet readers (whole-run, not causal; `suspicion >= 2`; cited steps are the predicted commands) ran
on all 41 natural and 9 of 128 synthetic trajectories before the subscription routes were exhausted. Scored on
those 50 with identical trajectories for every arm:

| Arm (natural, n = 41) | Alarmed | Exact hit | Within 5 | Mean P / R / F1 | Cited before first gold |
|---|---:|---:|---:|---|---:|
| Stage 1 | 26 | 4 [3.9, 22.5] | 6 | 0.056 / 0.063 / 0.039 | 18 |
| Cascade | 6 | 1 [0.4, 12.6] | 1 | 0.012 / 0.012 / 0.012 | 6 |
| Facets | 25 | 20 (48.8%) [34.3, 63.5] | 25 | 0.210 / 0.270 / 0.159 | 12 |
| Union (cascade or facets) | 29 | 21 (51.2%) [36.5, 65.7] | 26 | 0.222 / 0.282 / 0.170 | 16 |

Synthetic subset (9): facets 4 exact hits, union 4. Facets alone beat the cascade by 19 exact hits on the same
41 trajectories; the cascade adds one trajectory to the union. Facets read the full transcript, so they locate
but do not prevent; the 12 trajectories with a facet citation before the first gold command are cited
evidence, not measured false alarms.

## `submission_tamper` (pre-registered rule: demote if cost > gain)

Gain (positives alarmed only by it): Goodfire 1, CheatBench 1, HackBench 0. Cost (benign traces alarmed only
by it): CheatBench 5, Goodfire 0, HackBench 0 (it never fires on ML-research commands). Cost 5 > gain 2:
demote. The DEV measurement with Kimi judging those cards (CM-11b) and its code change are separate.

## Limits

HackBench has only HACK rows, so false alarms are bounded only by the pre-hack count. Synthetic facet coverage
is 7%; routes were exhausted (Kimi five-hour window, muse until 2026-11-02). One judge family decides the
cascade headline; the Stage-1 ceiling makes that immaterial here. The corpus is ML-research agents with long
heredocs, unlike the coding-agent corpora the tripwires were built on, so these figures measure transfer, not
the monitor on its home distribution.
