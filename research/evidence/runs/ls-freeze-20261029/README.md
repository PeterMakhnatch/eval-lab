# LS-FREEZE: the Live Supervisor did not notice that every OMP lane froze

Run 2026-10-29 (ET evening). Every number was measured by the LS-FREEZE worker from the recorded files and
the live machine named below; `results.json` has the same numbers in machine-readable form.

## Question

Why did the shadow Live Supervisor record no decision about any lane after (1) the 5:54:55 PM ET stop of 12 lane
`omp` processes and (2) the 1:36 to 8:45 AM host sleep, and can deterministic, model-free detection fix that
inside the existing trigger/decision contract?

## Cause (read from code and the recorded cycles and decisions)

- `live_supervisor_fleet._lane_hits` fires `silence` only for `agent_status == "working"` (or idle/done with
  pending items); a stopped lane's pane reports `agent_status: unknown`.
- `FleetSource.observe` joined a pane to its session through `pane.agent == "omp"` and herdr's
  `agent_session`. A frozen pane has neither (`agent` is absent), so no session was read, no heartbeat existed and
  `fleet-state.json` `baselines` was empty at the last cycle. `wall_time` needs an item 3 h old.
- Recorded: `cycles-v1.jsonl` continues to 22:55:15Z (25 cycles after the freeze), and `decisions-v1.jsonl` has 0
  lane decisions after 21:54:55Z (the last were `nudge` for `w1C:pV`/`w1C:p13` at 21:34-21:44Z).
- Host sleep: the first cycle after the wake (12:46:54Z, wake 12:45:43Z by `pmset`) recorded 9 lane decisions from
  12 `wall_time`/`silence` hits that were the sleep (ages of 7 h).

## What changed

New trigger `frozen` (kind `infra`, always `alert_research`; a `queue-append.jsonl` nudge cannot reach a stopped
or background process) with four deterministic signals: (a) `omp` or a descendant is job-control stopped,
(b) `omp` is alive outside the pane's foreground process group, (c) `agent_status=unknown`, item in flight, turn
open and no session event for 60 min, (d) the supervisor's previous cycle is 15 min or more old (the host slept:
heartbeats older than the wake are counted from it, one `frozen` hit per in-flight lane). Probes: `herdr pane
process-info --pane <id>` and `ps`; process state only. See `docs/live-supervisor.md` "Frozen lanes".

## What ran

| Script | What | Reads |
|---|---|---|
| `replay_frozen.py` | the shipped trigger over the recorded sessions of 11 authorized lanes, a cycle every 150 s from 10-28 12:00Z to the freeze, host sleep excluded, six heartbeat thresholds; plus the 12:46:54Z wake cycle before and after | keeper `_queue.log`, session timestamps/tool names/timeouts of lanes the fleet source already reads (never content), `windows-v1.jsonl` metrics |
| `live_probe.py` | `FleetSource.observe` with the real runner, throwaway state file, 4 passes between 23:12Z and 23:20Z | `herdr pane list`, `tab list`, `pane process-info`, `ps`; read-only |

## Numbers

Incident 1 (5:54:55 PM ET stop):

- Before: 0 lane decisions in 25 cycles after the freeze.
- After (a)/(b): 12 of 12 frozen panes flagged in 4 of 4 live passes. The first recorded cycle after the freeze
  was 110 s later (recorded spacing 123 to 330 s, so latency is bounded by one cycle). Historical process tables
  were not recorded: this is the cadence applied to the state observed live, not a replay of that evening.
- The omp states moved from 4 `T` + 8 `S` (brief, 6:38 PM) to 12 `T` (probed from 7:12 PM). [INFERENCE] A background
  `omp` that reads the terminal receives SIGTTIN and stops; not verified.
- (c) alone, replayed on recorded sessions, would have fired for 3 of 11 lanes (`wK:p1J` 3,005 s, `w1C:pS` 2,405 s,
  `w1C:pJ` 3,155 s after the freeze); the other lanes had no item in flight, an ended turn or an open `ask`.

Incident 2 (host sleep): detection is possible only at the wake, because the supervisor slept too. The first
cycle (71 s after the wake at most) previously recorded 9 lane decisions (7 `continue`, 2 `alert_research`)
from 9 `wall_time` and 3 `silence` hits. With the clock rule the same cycle gives 9 `frozen` hits (all
`alert_research`, "supervisor previous cycle 7 h ago") and 0 `wall_time`/`silence` hits. The count of lanes with a
decision is the same (9); the cause is now correct.

Alarms on legitimate idleness:

- (a)/(b): 0 of 32 observations of 8 healthy interactive `omp` panes (idle or blocked, in the foreground),
  4 passes; 6 panes with no `omp` never flagged; 0 unreadable probes.
- (c): 7,062 lane-cycles, 2,504 with no item in flight (waiting for dispatch: 0 alarms by construction), 4,558 with an item in
  flight. Heartbeat threshold sweep, episodes (alarm lane-cycles): 10 min 183 (1,196), 15 min 136 (855), 20 min
  121 (594), 30 min 4 (162), 45 min 3 (143), 60 min 1 (133). Shipped: 60 min, 1 episode (`wK:p1J`, 6 h). A further
  514 lane-cycles quiet for an hour were left alone: 426 turn ended, 88 open `ask`, 0 inside a declared tool timeout.
- (d): 93 recorded inter-cycle gaps: min 123 s, median 162 s, max 330 s; 0 at or above 15 min. These cycle records
  start at 3:00 PM ET on Oct 29, after the 8:45 AM wake, so they show the post-wake cadence; no cycle record covers
  the period before the sleep (correction from the fleet's independent verification, 2026-10-29).

## Meaning

The supervisor now alarms within one cycle on a stopped or background `omp`, even when herdr has lost the agent
and the session cannot be read, and no longer converts a host sleep into hours of "silence" or wall time.

## Limits

- No historical `ps`/herdr process tables exist, so incident-1 latency is cadence arithmetic plus live state.
- Heartbeat episodes are classified by tool and last record kind only; no content was read. The status `unknown`
  is assumed for every replayed cycle (what the frozen panes showed); a lane herdr still sees as `working` keeps
  the 30-min `silence`.
- 11 lanes, 1.3 days, one host. 60 min was the smallest threshold with one episode, not a tuned optimum.
- `SUPERVISOR_GAP_S` has one true positive (the 7 h sleep) and no false one in 93 gaps; the supervisor's cadence
  can change, and the constant must then be revisited.
- Shadow mode only: nothing was acted on, the installed plist and the running service are untouched; the running
  service does not load this code until it is restarted.
