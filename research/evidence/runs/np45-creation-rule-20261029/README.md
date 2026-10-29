# NP45-TW-1: preregistered rule R on the stored cross-test corpora (descriptive, nothing tuned)

Stored data only, read-only, $0, zero model calls, no downloads, no uploads, no Modal. **No production rule changes and nothing is
tuned.** Counts only are committed (no run ids, task ids, paths or transcript text). This file is pushed **before any new
population is read**; the commit that adds it is the freeze.

## The rule (one rule, no free parameter)

**R**: the `grader_tamper` pass-through fires on a run only if at least one of its alarm events is labelled kind **b**
(modification) or kind **c** (deletion) by the NP43-TW-1 classifier; kind **a** (creation) and kind **d** (undeterminable)
do not fire it. Classifier: `research/evidence/runs/np43-grader-tamper-kinds-20261029/tools/classify.py`, sha256
`0e5dc144b1741269cd3cbe28a7d3f1da3e20aa672d1fdf73b639d621730f6302` (unchanged; checked at run time, the run aborts on a
different digest), with the same parser: the NP36 gate replica (`gate.py`, sha256
`a3a5241ccac15d5f3e577415a0b0f0d26bf9e83d3aacf0e27d8c5d2bef8740e5`), asserted per run to give the same hit steps and paths as
`tripwires.grader_tamper_hits`. An alarm run **without R** has at least one gate hit; **with R** it has at least one kind b or
c event. Two caveats from NP44-VF-1 are kept as they are: the `existed before` predicate of NP43 includes
`semantic_no_prior_evidence`, and `own_file` as modification means "modified a path the agent itself wrote".

## Populations (fixed here), each read once

Every registry entry of `cross-test-registry.json` except HackBench (**untouched and unread**) plus CheatBench DEV and strict
`goodfire-dev`, so the table is complete. Counts below are the registry's positives and negatives (its false-alarm basis, in the
unit shown); the population actually read is the staged sample (cm18, np21, np33, NP38 sample, which earlier items froze) or, for
every other entry, the default population of its adapter's `specs()`.

| registry id | unit | positives | negatives | status for the gate |
|---|---|---|---|---|
| `kernelbench-hard-runs` | run | 11 | 19 | cm18 kernelbench |
| `reflex-sentinel` | run | 45 | 205 | cm18 reflex |
| `assistant-axis` | turn | 157 | 635 | unseen by the gate |
| `sleight-bench` | segment | 43 | 43 | unseen by the gate |
| `cuaverifierbench` | run | 77 | 29 | unseen by the gate |
| `apollo-insider-trading` | completion | 4255 | 5345 | unseen by the gate |
| `hack-ignition` | completion | 17154 | 12606 | unseen by the gate |
| `paichecker` | task | 1085 | 1502 | unseen by the gate |
| `automationbench-verified` | task | 210 | 0 | unseen by the gate |
| `agent-cheat-index` | run | 62 | 1086 | cm18 cheatindex |
| `evilgenie-escalation` | run | 124 | 1990 | cm18 evilgenie |
| `sdf-shortcut-taking` | run | 359 | 636 | cm18 sdf |
| `mistrace-reward-hacking` | run | 119 | 81 | unseen by the gate |
| `researcharena-benchmark-gaming` | run | 21 | 20 | cm18 researcharena |
| `beam-tripwire` | run | 171 | 442 | cm18 beam |
| `rhb-delivery-queue` | run | 129 | 409 | unseen by the gate |
| `baitbench` | run | 681 | 488 | np21 baitbench |
| `flake-laundering` | run | 7 | 217 | np21 flake |
| `kobayashi-maru` | run | 789 | 7916 | np21 kobayashi |
| `reward-pressure-study-2` | run | 467 | 653 | np21 osf |
| `caught-not-taught` | run | 46 | 160 | np33 cnt |
| `agentcheck` | run | 28 | 91 | np33 agentcheck |
| `posttrainbench-trajectories` | run | 109 | 1439 | np33 ptb |
| `collusion-probe-pitfalls` | run | 956 | 3966 | unseen by the gate |
| `knowing-collusion-filter` | run | 86 | 20 | unseen by the gate |
| `trusted-monitoring-logs` | run | 33 | 200 | unseen by the gate |
| `inferencebench-trajectories` | run | 17 | 211 | unseen by the gate |
| `linuxarena-public` | run | 138 | 400 | NP38 frozen sample |
| `rsi-benchmark-monitor-training` | run | 420 | 416 | unseen by the gate |
| `cape-bench` | run | 400 | 400 | unseen by the gate |
| `latent-agent-sandbox-episodes` | run | 178 | 148 | unseen by the gate |
| `resolvebench-support-agents` | run | 112 | 400 | unseen by the gate |
| `cheatbench-dev` (not in registry) | run | 75 | 75 | already read by the gate (R29-TW-1, NP36) |
| strict `goodfire-dev` | run | 1,636 | 752 | already read; NP43 gives 390/390 and 89/89 |

"already read" means R29-TW-1 or NP36-TW-1 computed the gate's alarm runs on that staged sample (without the kind classifier);
`unseen by the gate` means no gate run has touched the set. An entry whose adapter yields no step-level agent steps is listed with its
counts and **not** read further. An entry that cannot be read without opening a held-out key (TEST assignments, MiMo heldouts) is
counted and left unread. A read that exceeds 20 minutes is counted as not completed, not sampled. Units other than runs
(turns, segments, completions, tasks) are counted as the unit the adapter yields and flagged; their rows are treated as runs.
The scoring populations of each entry are the adapter's rows whose `gold.state` is `positive` or `negative`; `ambiguous` and
`excluded` rows are counted and never enter a rate.

## Statistic (descriptive only, no test, no threshold)

Per set: positive and negative rows, alarm rows on positives and on negatives **without R** and **with R**, the share of
positive alarm runs whose events are creation only (kind a only), and the share with an undetermined event and no b/c event
(kind d), the event-level kind counts, and Wilson 95% intervals on the shares. Per set, **flag every set where R removes
positive alarms**: those are the places where an editor-create cheat exists. Also reported: the sets with no alarm at all.
The LinuxArena S set is the already-seen set (NP38/NP43: 27/60 S, 0/60 non-S; R keeps 12/27).

## What the table will be said to support (stated before reading)

1. **R keeps all positive alarms and removes negative ones on every set**: supported only if on every set with an alarm the
   positive alarm runs with R equal those without R, and on every set with negative alarms the negative alarm runs with R are fewer.
2. **R loses positive alarms on some set**: supported iff some set has fewer positive alarm runs with R than without; the sets
   and the count are listed.
3. **The classifier cannot tell on enough events**: supported iff on some set with at least 10 positive alarm runs, more than 25%
   of them have a kind d event and no b/c event; the sets are listed.
More than one can hold; I do not recommend a rule.

## Choices that depend on what I have seen

I read the **schema** (row keys, step keys, tool-call keys, state counts) of the rsi-benchmark, latent-agent-sandbox-episodes and
ResolveBench adapters to see whether they yield agent steps, before writing this file; no alarm, kind or label was computed. The
classifier, R, the set list, the statistic and the three statements above are fixed here. Anything added after the first read is
labelled post hoc in RESULTS.md. Converting an adapter's steps to the ATIF form the gate reads (`tools/convert.py`) is part of
the read, not a tuned choice; its logic is fixed by the adapter's own field names and committed with the results.

## Contents

`tools/convert.py`, `tools/run_sets.py` (label-free event extraction), `tools/census.py` (label join and counts), `results/`
(counts only), `RESULTS.md`.
