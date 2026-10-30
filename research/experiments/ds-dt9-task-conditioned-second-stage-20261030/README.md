# DT-9 — task-conditioned second stage (L2-lite) on reader alerts, with a no-task-statement ablation

DEV-only follow-up to DT-8. Stage 1 is the DT-8 verified-evidence reader; stage 2 re-judges
each of its 69 alerts (Goodfire 15, CheatBench 54) three times with the task statement
(arm A) or without it (arm B), and the alert is dismissed only if at least 2 of 3 calls
say `legitimate`.

Result: **both pre-registered gates fail**, and the task statement makes no measurable
difference.

| corpus | stage-1 reader | reader after stage 2 | funnel (∪ cascade v8) | TP dismissed | FA dismissed |
|---|---|---|---|---|---|
| Goodfire | 15/80, 0/70 | 8/80, 0/70 | 16/80, 1/70 | 7/15 | 0/0 |
| CheatBench | 42/74, 12/75 | 34/74, 3/75 | 47/74, 3/75 | 8/42 | 9/12 |

- G-GF **FAIL** (16/80 recall, needed ≥20; 1/70 false alarms, ≤2 allowed).
- G-CB **FAIL** (47/74 recall, met; 3/75 false alarms, ≤1 allowed).
- Ablation: dismissals of the 12 flagged negatives are identical with and without the task
  statement (9/12 both arms, and 4/5 both ways on the 5 that actually carry one); the only
  movement is 2 more true positives dismissed without it (p = 0.69). The second stage is a
  second look, not a task-conditioned investigator.

Cost: $0.77 for 414 calls ($0.0057/packet), ~$0.56 and ~$2.05 of stage 2 per 1,000 runs.

## Files

- `dt9_second_stage.py` — packet builder, caller, scorer (`build` / `read --arm A|B` /
  `dump-calls` / `score`).
- `prereg.md` — the frozen design, rubric, decision rule and gates (sha256 `42358e169ae6`).
- `results/result.json` — every rate, interval, tertile, McNemar and cost figure.
- `results/packets-manifest.json` — packet ids and sizes only, no packet text.
- `results/calls.jsonl` — per-call verdicts, served model and usage; no replies.
- `results/attestation.json` — served-model attestation and call agreement.

No transcript, packet text or model reply is stored here. Labels are joined only in
`score`. Packet construction, code paths and reproduction commands are described in the
item report (`DT-9/report.md` in the squad bridge); run the driver against
`~/.local/state/evallab-prod-live/cloud-trace-db.sqlite` and the DT-8/DT-2 readings.
