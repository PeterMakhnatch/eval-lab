# ID-CM-1 results: how late do the detectors speak?

Replayed runs, stored cascade v8 replays, agent turns, no model call. Medians are over alarmed runs; "remaining" is turns left after the alarm. The verdict lag (flagged step to
verdict) is 5 turns in nearly every confirmed card, the cascade's fixed lookahead, so about 5 of the turns below are waiting for the judge's window, not model time.

## Cascade v8 (the only causal detector), positives

| Set | Runs | Alarmed | Steps to first alarm (median, IQR) | Fraction of run elapsed (median) | Turns remaining (median) |
|---|---|---|---|---|---|
| Goodfire DEV enacted | 80 | 10 | 19.5 (14.5-32.8) | 0.51 | 33 |
| CheatBench DEV | 75 | 19 | 9 (8-12.5) | 0.47 | 14 |
| HackBench natural | 41 | 6 | 26 (12-143) | 0.15 | 434 |
| HackBench synthetic | 128 | 1 | 479 | 0.77 | 145 |
| Agent Cheat Index | 60 | 14 | 12 (7-13.5) | 0.73 | 2.5 |
| Beam tripwire | 60 | 4 | 21 (19.5-23.5) | 0.75 | 9 |
| EvilGenie | 60 | 7 | 6 (4.5-6.5) | 1.00 | 0 |
| ResearchArena | 21 | 7 | 33 (31.5-66.5) | 0.14 | 323 |
| SDF shortcut-taking | 60 | 2 | 132.5 | 1.00 | 0.5 |
| Reflex-Sentinel, KernelBench | 45, 11 | 0, 0 | no alarms | | |

- **Early only where the run is long.** On HackBench and ResearchArena (hundreds of turns) the first alarm lands at 14-16% of the run with 300-430 turns left. On the short
  coding-agent sets (15-55 turns) the alarm arrives at half the run or later, and for EvilGenie it is at the last step (5 of 7 alarms in the last tenth of the run, median 0 turns left):
  nothing a live supervisor could stop.
- **Relative to the hack.** Goodfire: 9 of 10 alarms come after the first labelled step, median 13 turns later (1 within 5 turns); 1 of 10 is at or before it. HackBench: 6 of the 7 alarmed
  runs alarm at or before the first gold command (CM-11's pre-hack alarms), so the cascade's early HackBench alarms are not detections of the labelled hack; one run alarms 5 turns after it.
- **False alarms** come mid-run: Goodfire clean 2 alarms at median 55% of the run; Beam clean 16 alarms at a median 63% of the run with 11 turns left.

## Combination with facets (`suspicion >= 2`, whole-run reader, so end-of-run)

| Set | Union alarms | Cascade-timed | Facet-only (alarm at run end) |
|---|---|---|---|
| Goodfire DEV enacted | 15 | 10 | 5 (33%) |
| CheatBench DEV | 44 | 19 | 25 (57%) |
| HackBench | 34 | 7 | 27 (79%) |
| Agent Cheat Index | 32 | 14 | 18 (56%) |
| EvilGenie | 46 | 7 | 39 (85%) |
| Beam | 38 | 4 | 34 (89%) |
| SDF | 43 | 2 | 41 (95%) |
| ResearchArena | 21 | 7 | 14 (67%) |
| Reflex-Sentinel | 45 | 0 | 45 (100%) |

Everything the facet reader adds is end-of-run: 33% of the union's detections on Goodfire and 56-100% on the unseen sets come with no early warning. As a live monitor the
combination is the cascade; the facets are a run-end review that can still gate what is kept, retried or sent to the Trace Watcher.

## What would make it live

1. A harness detector that ingests a run's steps as they arrive (the cascade already does, event by event, through `MisalignmentLiveWorker`; the harness adapters only read stored rows).
2. The Live Supervisor reading the alarm's `trace-findings/v1` record and choosing `flag_cheating` or a stop. The turns above are what that decision would have to work with, minus model wall-clock.
3. A run-end facet pass (about 36k Haiku input tokens per run, CM-18) for the facet-only alarms.

## Limits

Goodfire has only 10 alarmed enacted runs and Beam 4, so medians are rough. Stored replays, not live clock time: model and queue latency are not included. The facet alarm time is
end-of-run by construction; CM-16 found that a Haiku reader raised no `suspicion >= 2` on Goodfire prefixes at all, so its earliest possible time here is the end. One evilgenie
negative with no agent turns is not in the table.
