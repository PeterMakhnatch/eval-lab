# TT-3: own-run cascade and existing Laminar Signals

## Question

Do whole-run Laminar Signals detect reward-hacking runs that the deterministic
rule prefilter or its bounded-window confirmation cascade misses?

## What ran

- Frozen own-run cohort: **169 Harbor runs, 104 positive / 65 negative**. Labels,
  cohort and family were joined only by `score.py`, never added to a prompt or
  Cloud request. The **94 cheatbench-port runs (84 positive / 10 negative)** are
  reported separately from **75 other runs (20 positive / 55 negative)**.
  The latter include 25 trace-paired DEV runs; the contamination-audit runs are
  unsplit and must not be represented as benchmark DEV performance.
- Stage 1: `MisalignmentLiveWorker(emit=..., judge=None)`, one `trace.step` event
  per actor-visible ATIF step. The replay followed two continuation chains,
  omitted eight copied-context steps, and processed **5,517 steps**. Step IDs
  are consecutive stitched IDs; local `cascade-own-sources.jsonl` maps them to
  original file/step IDs. No task manifest, publisher metadata, reward, labels,
  split or family was supplied to the worker.
- Stage 2: the production `MisalignmentLiveWorker` / `window_cascade` code path,
  frozen **CM-9 `pf_r`, window-cascade/v7** arm: all seven prefilter families,
  flagged-step reasoning, no context reasoning, no Stage-2 playbook/reference
  examples, 9,000 free-text characters, radius/lookahead 5, three families and
  at most one diagnostic family. `close()` flushes deferred windows. An alarm
  is **any confirmed card OR `grader_tamper`/`submission_tamper` pass-through**.
- **Partial judge qualification: v7 arm, grok judge (not production judge).**
  The lane owner reported Kimi Code's five-hour quota exhausted (latest reset
  update: **10:22 PM ET on October 27**). The authorized SuperGrok alternate
  produced **43 successful card verdicts from 56 completed call attempts**,
  then its weekly subscription quota was reported exhausted until October 30.
  The running batch was cancelled on that update. Retained verdicts were
  recovered with `--cache-only`, which makes no hosted calls; other alarms
  remain null. Kimi was deferred without a call, not scored negative.
  Route identity is checked on every successful assistant response; fallback,
  nonterminal responses and malformed answers are unavailable coverage.
  **Actual spend $0**. Known successful-call usage is **139,963 input /
  25,302 output tokens**; usage for the 13 failed attempts is unavailable,
  not asserted to be zero. Canonical batch records are in
  `~/.local/state/evallab-failure-library/spend.jsonl`, mirrored in the
  trace-tools ledger and this directory's `spend.jsonl`. No Muse/OpenRouter
  call or Laminar paid Signal execution was made.
- Laminar was **read-only**: map cached IDs to cohort trial names locally;
  query deterministic and native-SDK trace IDs via `GET /v1/signals` and SQL
  `SELECT`s over `signal_runs`, `signal_events`, and `traces`. No `/v1/traces`
  request, Signal execution, new ingestion, config change or BYOK model call.
  Event present = true; completed run without an event = false; failed/pending
  run = null. Conflicting historical flags would be null; none were found.
- Code/runtime baseline: worktree created from `af15ad90f` on `origin/main`.
  Runtime module hashes are in `runtime-source-sha256.json`; per-trajectory
  hashes are in `trajectory-source-sha256.jsonl`. Full private call outputs,
  source maps and Cloud readbacks remain under
  `~/.local/state/evallab-prod-live/trace-tools/`, not committed as raw prompts.

## Numbers

### Deterministic Stage 1

| Stratum | Positive / negative | TP | FP | Recall | False-alarm rate |
|---|---:|---:|---:|---:|---:|
| All own runs | 104 / 65 | 95 | 42 | 91.3% | 64.6% |
| cheatbench-port | 84 / 10 | 78 | 10 | 92.9% | 100.0% |
| Non-cheatbench-port | 20 / 55 | 17 | 32 | 85.0% | 58.2% |
| trace-paired DEV only | 14 / 11 | 12 | 8 | 85.7% | 72.7% |

Stage 1 raised **903 cards across 137 runs**. A Stage-1 card is a lead, not a
confirmed cheating verdict.

### Partial final alarms: Grok-qualified, not production judge performance

The final view covers **all 169 runs**: **53 determinate alarms** (15 true /
38 false) and **116 nulls with explicit unavailable-judge reasons** (83
positive / 33 negative). **19 runs contain completed Grok judge cards, 43
cards in total**; the other determinate alarms include deterministic
pass-through and no-Stage-1-lead cases and must not be called 53 model readings.

| Stratum | P/N scored | TP / FP | Conditional recall / FAR | Null alarms |
|---|---:|---:|---:|---:|
| All own runs | 21 / 32 | 11 / 4 | 52.4% / 12.5% | 116 |
| cheatbench-port | 6 / 0 | 0 / 0 | 0.0% / — | 88 |
| Non-cheatbench-port | 15 / 32 | 11 / 4 | 73.3% / 12.5% | 28 |
| trace-paired DEV only | 14 / 9 | 11 / 1 | 78.6% / 11.1% | 2 |

These rates describe a **quota-limited, nonrandom partial slice**, not
full-cohort cascade performance. In particular, no determinate
cheatbench-port negative denominator exists. End-to-end observed TP / all
positives is 11/104 overall, a coverage lower bound rather than estimated
production recall.

For copied-upstream versus determinate final alarms, the positive overlap is:

| Stratum | Both | Laminar-only TP | Cascade-only | Neither | Unknown comparison |
|---|---:|---:|---:|---:|---:|
| All own runs | 11 | 6 | 0 | 1 | 86 |
| cheatbench-port | 0 | 5 | 0 | 1 | 78 |
| Non-cheatbench-port | 11 | 1 | 0 | 0 | 8 |

Thus there are **six confirmed comparison-level complementary TPs**:
five prefilter misses in cheatbench-port and one non-port positive missed
by the completed Grok-qualified cascade. Another **18 copy-positive runs
have unknown cascade alarms** and are **not** counted as cascade misses.
False-completion, stuck-loop and infra each add zero determinate final-alarm
TPs; Failure Detector adds two (one port, one non-port), with six additional
negative alarms among runs where the cascade is false. Full per-Signal,
per-family overlap counts and run IDs are in `summary.json`.

### Existing Laminar coverage

**145/169 runs** have at least one determinate historical Signal result;
**145/169** have an existing Cloud trace. The three enabled Eval Lab Signals
cover **143 runs for copied-upstream**, **142 for false-completion**, and
**143 for stuck-loop**; **142 runs** have all three determinate. Cached verdicts
contributed **42 observations**; the final merged readback contains **608
(run, Signal) records** across ten Signal names, including failed abstentions.
There was no new ingestion and no new model spend.

Recall/FAR below use only determinate positives/negatives. `P/N scored` shows
those denominators; abstentions include entirely missing runs and are never
negatives. The machine-readable scores also give end-to-end TP / all positives
so incomplete coverage cannot masquerade as full-cohort recall.

| Stratum | Signal | P/N scored | TP / FP | Recall / FAR | Abstentions |
|---|---|---:|---:|---:|---:|
| All | copied_upstream_fix | 99 / 44 | 35 / 0 | 35.4% / 0.0% | 26 |
| All | false_completion_claim | 99 / 43 | 7 / 1 | 7.1% / 2.3% | 27 |
| All | stuck_loop | 99 / 44 | 16 / 23 | 16.2% / 52.3% | 26 |
| All | infra_not_model (currently disabled) | 8 / 23 | 3 / 5 | 37.5% / 21.7% | 138 |
| All | Failure Detector preset | 97 / 44 | 68 / 34 | 70.1% / 77.3% | 28 |
| cheatbench-port | copied_upstream_fix | 84 / 10 | 20 / 0 | 23.8% / 0.0% | 0 |
| cheatbench-port | false_completion_claim | 84 / 9 | 5 / 1 | 6.0% / 11.1% | 1 |
| cheatbench-port | stuck_loop | 84 / 10 | 11 / 5 | 13.1% / 50.0% | 0 |
| cheatbench-port | infra_not_model | 0 / 0 | 0 / 0 | — / — | 94 |
| cheatbench-port | Failure Detector preset | 82 / 10 | 53 / 9 | 64.6% / 90.0% | 2 |
| Non-cheatbench-port | copied_upstream_fix | 15 / 34 | 15 / 0 | 100.0% / 0.0% | 26 |
| Non-cheatbench-port | false_completion_claim | 15 / 34 | 2 / 0 | 13.3% / 0.0% | 26 |
| Non-cheatbench-port | stuck_loop | 15 / 34 | 5 / 18 | 33.3% / 52.9% | 26 |
| Non-cheatbench-port | infra_not_model | 8 / 23 | 3 / 5 | 37.5% / 21.7% | 44 |
| Non-cheatbench-port | Failure Detector preset | 15 / 34 | 15 / 25 | 100.0% / 73.5% | 26 |

The other five historical presets have only **one negative run each and no
positive coverage**: Friction Detector, Intent Classifier, Logic Analyzer and
Task Evaluator each fire once (one FP); Hallucination Detector does not (one
TN). They abstain on the other 168 runs. Their per-stratum and per-family
scores are retained in `summary.json`, not promoted as calibrated detectors.

### Complementarity already established against Stage 1

`copied_upstream_fix` finds **five positive runs with no Stage-1 card**, all
within cheatbench-port. It finds **zero additional positives outside that
stratum** among the currently observed Laminar results. False-completion and
stuck-loop each add zero positive Stage-1 misses. The broad Failure Detector
preset adds one different positive Stage-1 miss, also cheatbench-port; it is
not a reward-hacking-specific detector and its false-alarm rate is high.

### Exact specified backfill arithmetic, not a spend authorization

The **24 not-yet-ingested cohort runs** contain **6,835,129 trajectory
characters**, counting root plus continuation documents. At the requested
characters / 4 heuristic this is **1,708,782.25 input tokens per Signal**.

The observed `/v1/signals` config has **three enabled realtime Eval Lab
Signals**, all `glm-5.3-flash` on `evallab-zai`: copied-upstream,
false-completion, stuck-loop. `infra_not_model` is realtime but **disabled**
and therefore not counted. The observed `disabled` field, not the mere
`mode=realtime`, is authoritative for this count.

- Input: 1,708,782.25 × 3 = **5,126,346.75 tokens** × $0.15/M = **$0.7689520125**.
- Output: 24 × 3 × 1,000 = **72,000 tokens** × $0.50/M = **$0.036**.
- **Z.ai provider estimate: $0.8049520125** (about $0.805).

Laminar's enabled realtime **Failure Detector** preset would also fire on
traces over 1,000 tokens. Its own credit/overage cost is **not included or
verified**. The figure above is exact arithmetic under the requested heuristic,
not an all-in quote: prompt/serialization overhead, span projection/truncation,
provider tokenization and caching can change actual usage. Two already-ingested
runs lack enabled-Signal results and another has one failed Signal; the 24-run
estimate is for new ingestion, not a promise to complete every missing Signal
on already-ingested traces. No backfill was performed; **actual spend $0**.
Per-run inputs and costs are in `laminar-backfill-per-run.json`.

## Meaning

Whole-run copied-upstream analysis has demonstrated complementary detection:
**five positives missed by Stage 1, all cheatbench-port**, plus **one
non-cheatbench-port positive missed by the completed Grok-qualified final
cascade**. The latter is evidence about this alternate judge, not the
unavailable production/Kimi judge. The 75 non-cheatbench-port runs remain
separate; there was no additional Stage-1 positive there. The existing copy
Signal has no false positives in the 44 covered negatives, whereas broad
loop/failure signals have many. Absence of a card, absent Cloud data and
failed judging are three different things and are not collapsed together.

This is an **own-run, retrospective coverage/overlap measurement**, not a
controlled fresh common-input tool trial. Historical Laminar spans were not
regenerated or retroactively blinded in this readback, and input equivalence
with the new bounded-window replay is not established. Most positives are
cheatbench-port, Signal coverage is incomplete, and the substituted Grok judge
is not production-judge evidence. No held-out corpus was uploaded or tuned on;
no precision/recall claim should be generalized to production traffic.

## Reproduce and locate receipts

From this owned worktree (Python 3.12; no Docker/local inference):

```sh
uv sync --locked --no-default-groups
EVALLAB_DISABLE_LOCAL_DOCKER=1 PYTHONPATH=src .venv/bin/python \
  research/evidence/runs/trace-tools-tt3-20261027/cascade-laminar/cascade_own.py \
  --stage1-only --jobs 3
keys run -- .venv/bin/python \
  research/evidence/runs/trace-tools-tt3-20261027/cascade-laminar/laminar_readback.py
EVALLAB_DISABLE_LOCAL_DOCKER=1 PYTHONPATH=src .venv/bin/python \
  research/evidence/runs/trace-tools-tt3-20261027/cascade-laminar/cascade_own.py \
  --model xai-oauth/grok-4.3 --cache-only --jobs 3
.venv/bin/python research/evidence/runs/trace-tools-tt3-20261027/cascade-laminar/score.py
```

### Resume pending Stage 2 with Kimi after the reset

Run this **one command at/after 10:22 PM ET**. It skips every already-resolved
run (including retained Grok-confirmed and deterministic pass-through/no-lead
alarms), uses cached Kimi answers on interrupted runs, disables fallback, and
starts no new calls after a quota/credit-limit response. The final view is
always one row per run; unjudged coverage remains `alarm=null` with a reason.
At most the other already-in-flight calls can finish after a quota response.

```sh
EVALLAB_DISABLE_LOCAL_DOCKER=1 \
  /Users/petermakhnatch/Developer/eval-lab/.worktrees/trace-tools-own-cascade-20261027/.venv/bin/python \
  /Users/petermakhnatch/Developer/eval-lab/.worktrees/trace-tools-own-cascade-20261027/research/evidence/runs/trace-tools-tt3-20261027/cascade-laminar/cascade_own.py \
  --state /Users/petermakhnatch/.local/state/evallab-prod-live/trace-tools \
  --model kimi-code/k3.1-pro-highspeed --jobs 3
```

This runner has no timer and is not currently waiting for the reset. It appends
actual batches to the canonical and trace-tools spend ledgers. A later score
must preserve each row's judge qualification and be reported separately from
the frozen partial-Grok evidence here; do not call a mixed result production
judge performance. To render that later score without rewriting this evidence,
run `score.py --out /tmp/trace-tools-kimi-resume-score-20261027`.

- `cascade-own.jsonl`: exactly one final row per cohort run; triage errors and
  null-alarm reasons retained. Private state has the same final view.
- `laminar-own-flags.jsonl`: compact flags, statuses and provenance. Full contract
  rows were appended once per identity to private `results-v1.jsonl` with
  scan `tt3-own-laminar-readback-20261027`.
- `summary.json`: every detector, cohort/family stratum and exact per-positive
  overlap with Stage 1 and final alarm. Missing/failed flags remain null.
- `cascade-receipt.json`, `laminar-readback-receipt.json`,
  `laminar-signals.json`, `kimi-quota-deferral.json`, `grok-quota-stop.json`,
  `judge-call-receipts.jsonl`, `spend.jsonl`: execution, live config,
  served-model attestation, blocked-route reasons and zero-dollar accounting.
