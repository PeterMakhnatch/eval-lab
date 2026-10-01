# HAR-126 G0: lf2 loop-break replay, sweep, and freeze decision

Owner: Engineering (G0, due 05:15Z). Script: [`replay_lf2.py`](replay_lf2.py)
(`uv run --no-sync python research/experiments/har126-lf2/replay_lf2.py`).
Per-run rows: [`replay.csv`](replay.csv) (112 rows).
Sweep rows: [`replay_sweep.csv`](replay_sweep.csv) (45 settings).
Winner: [`winner.json`](winner.json).

## Decision: ship the break at 8 / 20 / 15, not cap-only

The sweep finds settings that cut **no run that scored 1**, so lf2 keeps the
loop break (it is not cap-only). The winner is the zero-cut setting that
saves the most input tokens:

| setting | command_run_min | message_run_min | grace_calls | scored-1 cut | input tokens saved |
|---|---|---|---|---|---|
| **lf2** | **8** | **20** | **15** | **0 of 24** | **50,149,859 (~50.1M)** |

At the winner, over all 112 runs: 63 nudged, 44 stopped, 0 scored-1 runs
cut. Seven scored-1 runs are stopped *after* their last real edit (pass
kept, loop dropped); three scored-0 runs are stopped before their last
edit (nothing lost — none passed). One HAR-110 dev run has no recorded
reward; its stop (36) lands after its last edit (3), so it is kept under
either reading.

## lf2 tree

- Path: `research/experiments/har126-lf2/harness-lf2/`
- Digest (lab `load_harness_tree` / `evidence_tree_digest`):
  `sha256:f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be`
- Knobs (`terminus/config.json`): `loop_break=true`,
  `output_cap_chars=2000`, `loop_command_run_min=8`,
  `loop_message_run_min=20`, `loop_grace_calls=15`.
  Everything else is byte-identical to the HAR-116 loopfix tree
  (`sha256:06e5712c…`).
- The threshold knobs are new first-class adapter kwargs
  (`SecretSafeTerminus2`), validated in `terminus_harness` (integers ≥ 2)
  and defaulting to the HAR-114/116 constants (4/10/5) when unset, so old
  trees behave exactly as before.
- Live/replay agreement is now structural: on parse-error turns the live
  agent detects on the raw model response — the exact bytes upstream
  records as the step message — and runs the same `live_loop_action` rule
  there (nudge rides the parse-error feedback; the stop raises at the
  per-episode dump). Behaviour test:
  `test_parse_error_loop_nudges_on_raw_response_then_stops` replays the
  001896 shape (repeated unparsed `<tool_call>` markup, varying
  analysis/plan) and fails on the pre-fix code.

## Corpus (112 runs, $0 read-only)

| source | runs | reward 1 | reward 0 | no reward |
|---|---|---|---|---|
| HAR-104 (har104-d-*, via har114 runs.jsonl) | 10 | 3 | 7 | 0 |
| HAR-110 (all trials: 12 plain/seed/gepa + 5 dev + 11 gepa format-code) | 28 | 7 | 20 | 1 (dev-002256) |
| HAR-81 (44: l-d-a2/a3/a4 + p-d) | 44 | 7 | 37 | 0 |
| HAR-116 (10 baseline + 10 loopfix-r2 + 10 Part B) | 30 | 7 | 23 | 0 |
| **total** | **112** | **24** | **87** | **1** |

Decisions use `evallab.loopfix.loop_decision` — the same code the live
path calls. Cuts compare the stop call against `token_flow`
last_useful_edit (HAR-114 rows reuse their embedded token_flow; HAR-116
rows compute it fresh; numbering verified: 000587-loopfix-r2 replays to
nudge 61 / stop 66, and 001896-loopfix-r2 to 53 / 58, matching the live
record and the offline finding). Savings = prompt input tokens after the
stop call. A stop before the last edit of a reward-1 run is a cut.

## What the winner does to the runs the card asked about

- HAR-81 2684 (l-d-a2, earned 78): nudged 45, loop **broke at 60** — no
  stop. Kept (was cut 46→51 at 4/10/5).
- HAR-81 1271 (l-d-a2, earned 91): nudged 45, loop **broke at 60** — no
  stop. Kept (was cut 41→46 at 4/10/5).
- HAR-81 2684 (p-d, reward 1, edit 78): nudged 56, broke 69 — kept.
- HAR-104 002391 (edit 8): nudged 23, stopped 38 — kept.
- HAR-116 000587-loopfix-r2 (reward 1, edit 53): nudged 65, window still
  open at call 66 — kept.
- HAR-116 001896-loopfix-r2 (reward 0): nudged 63, stopped 78
  (identical_message_run) — the fixed live detector fires here too.
- HAR-116 002308-leakclosed (raw reward 1, edit 48): never fires — kept.
- Scored-1 runs stopped after their edit (7, all kept): HAR-104 000226
  (stop 46, edit 11), 002391 (38/8), 002864 (61/12); HAR-81 1520-a2
  (42/7), arvo-18737-a3 (78/33); HAR-116 002308-original (91/24),
  002402-original (64/23).
- Scored-0 runs stopped before their edit (3, nothing lost): HAR-110
  000495-plain (stop 67, edit 81), gepa df7dfd07 (48/79), HAR-116
  000226-leakclosed (44/104).

## Sweep table (all 45 settings)

`cmd` = command_run_min, `msg` = message_run_min, `s1` = scored-1 cut,
`s0` = scored-0 cut before edit, `saved` = input tokens saved (M).

| cmd | msg | grace | s1 | s0 | saved | | cmd | msg | grace | s1 | s0 | saved |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 6 | 10 | 5 | 2 | 4 | 71.3 | | 8 | 20 | 15 | **0** | 3 | **50.1** |
| 8 | 10 | 5 | 2 | 5 | 70.8 | | 4 | 10 | 20 | 1 | 4 | 50.2 |
| 4 | 10 | 5 | 2 | 4 | 70.1 | | 6 | 10 | 20 | 1 | 3 | 49.3 |
| 6 | 15 | 5 | 2 | 4 | 70.0 | | 4 | 15 | 20 | 0 | 3 | 47.8 |
| 8 | 15 | 5 | 2 | 5 | 69.6 | | 4 | 20 | 20 | 0 | 3 | 46.9 |
| 4 | 15 | 5 | 2 | 4 | 68.9 | | 6 | 15 | 20 | 0 | 2 | 46.8 |
| 6 | 20 | 5 | 2 | 4 | 68.7 | | 6 | 20 | 20 | 0 | 2 | 46.0 |
| 8 | 20 | 5 | 2 | 5 | 68.3 | | 8 | 10 | 20 | 1 | 3 | 45.0 |
| 4 | 20 | 5 | 2 | 4 | 67.6 | | 8 | 15 | 20 | 0 | 2 | 42.5 |
| 4 | 10 | 10 | 2 | 4 | 63.7 | | 8 | 20 | 20 | 0 | 2 | 41.7 |
| 6 | 10 | 10 | 2 | 4 | 63.6 | | 4 | 10 | 30 | 0 | 2 | 35.5 |
| 4 | 15 | 10 | 2 | 4 | 62.4 | | 4 | 15 | 30 | 0 | 2 | 34.7 |
| 6 | 15 | 10 | 2 | 4 | 62.3 | | 4 | 20 | 30 | 0 | 2 | 33.8 |
| 8 | 10 | 10 | 2 | 4 | 61.3 | | 6 | 10 | 30 | 0 | 1 | 33.7 |
| 4 | 20 | 10 | 2 | 4 | 61.0 | | 6 | 15 | 30 | 0 | 1 | 32.9 |
| 6 | 20 | 10 | 2 | 4 | 60.9 | | 6 | 20 | 30 | 0 | 1 | 32.1 |
| 8 | 15 | 10 | 2 | 4 | 60.0 | | 8 | 10 | 30 | 0 | 1 | 32.0 |
| 8 | 20 | 10 | 2 | 4 | 58.7 | | 8 | 15 | 30 | 0 | 1 | 31.2 |
| 4 | 10 | 15 | 2 | 4 | 57.1 | | 8 | 20 | 30 | 0 | 1 | 30.4 |
| 6 | 10 | 15 | 2 | 4 | 56.7 | | | | | | | |
| 4 | 15 | 15 | 2 | 4 | 55.8 | | | | | | | |
| 6 | 15 | 15 | 2 | 4 | 55.3 | | | | | | | |
| 8 | 10 | 15 | 1 | 4 | 54.0 | | | | | | | |
| 4 | 20 | 15 | 1 | 3 | 53.3 | | | | | | | |
| 6 | 20 | 15 | 1 | 3 | 52.9 | | | | | | | |
| 8 | 15 | 15 | 1 | 4 | 52.6 | | | | | | | |

Reading: cuts fall 2 → 1 → 0 as the grace window lengthens and the run
minimums rise. The current HAR-116 rule (4/10/5) cuts 2 scored-1 runs
(2684, 1271). No grace-30 setting cuts any pass, but each saves
15–20M fewer tokens than the winner. The winner's nearest zero-cut
neighbours save less ((4,15,20): 47.8M; (8,20,20): 41.7M).

## Limits

- The replay is counterfactual on recorded text: it assumes the model
  writes the same turns under the new rule. A nudge that breaks a loop
  early is not credited; neither is a model that repeats in a shape the
  detector misses.
- Savings count prompt input tokens after the stop; the nudge turn's own
  cost stays.
- Part B rewards are raw verifier rewards (the zero-rule would zero
  blocked-attempt passes; the adoption rule protects raw passes, the
  conservative choice).
- n=1 per cell everywhere: the thresholds are tuned on these 112 runs,
  not validated on held-out ones. The nearest zero-cut neighbours are
  listed above so a later gate can trade savings for margin.
