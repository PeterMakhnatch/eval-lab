---
status: living
audience:
  - operator
  - builder
---

# Live Supervisor

The Live Supervisor watches work while it runs and decides whether to step in. It is the
live counterpart of [Run Health](run-health.md), which scores finished runs, and it
reuses the per-trial watch in [live-watch.md](live-watch.md) rather than replacing it.

The loop has three stages:

1. Cheap deterministic triggers decide whether a subject needs attention.
2. Only for triggered subjects, a model reads the recent window and picks one action.
3. Every decision becomes a `trace-findings/v1` record with detector `live_supervisor`.

It starts in **shadow mode**: decisions are logged and nothing is acted on. Actions are
enabled per action only after adjudicated precision reaches 0.9 or better.

## What runs live and how to observe and stop it

Inventory as of 2026-10-28 at about 4:30 PM ET, from `ps`, `launchctl list`,
`modal app list --json`, `herdr pane list`, `omp usage` and the files named here.

| Subject | Started by | Observe | Stop | Supervisor may act? |
|---|---|---|---|---|
| Harbor trial (`evallab run`, queue dispatch, `evallab hack run`) | lab worker or lane | `runs/<job>/<trial>/agent/trajectory.json` (mtime, steps); `<job>/watch/{hooks,alerts}.jsonl`; `result.json` absent while live | `evallab watch stop <job> <trial>` writes `watch/stop-requests/<trial>.json`; the in-process actuator in `harbor_watch_hooks` cancels that trial only and refuses after verification starts | yes, lab job |
| Harbor job dispatch | `queue.py` | `queue/STOP`, `queue/events.jsonl`, `queue/running/` leases | `queue.stop()` fence (dispatch only); `evallab resume` clears it | yes, fence only |
| Modal apps started by lanes (`ts14-native-repairs-*`, `sb1-seeker-b-pilot`, `fl-verify-*`, `mimo-*`, `har*`, `2026-10-2*-*`) | lane scripts | `modal app list --json` (`state`, `tasks`); `modal app logs <id>`; spend ledgers such as `~/.local/state/evallab-prod-live/sb1/modal-spend-ledger*.json` | `modal app stop <app_id>` | yes, if the name is on the lab allow-list and the app is not `evallab-cloud-production` |
| `evallab-cloud-production` (Modal control plane) | `modal deploy` of `production/modal_cloud.py` | its ASGI dashboard; `/data/cloud-spend.json` | owner redeploy only | no, report only |
| launchd lab services: `run-atlas-watch` (300 s), `docent-watch` (600 s), `scout-watch` (300 s), `trace-store-*sync` (600 s), `sync-primary` (300 s), `tick`, `nightly`, `janitor`, viewers | LaunchAgents `com.petermakhnatch.evallab.*` | one status line per cycle in `~/Library/Logs/evallab/*.log`, `run-atlas/logs/run-atlas-watch.out`, `trace-tools/docent-watch-last.json`, `trace-store/watch-state.json` | `launchctl bootout gui/$UID/<label>` (KeepAlive services respawn on `kill`) | no; periodic and capped, so the supervisor reports a stuck no-op |
| `prod-live` runner (`LiveRunner`: execution, replay, monitor, scheduler) | LaunchAgent `…evallab.prod-live` | `prod.sqlite` journal, `service.heartbeat` every 15 s, leases | `launchctl bootout`; `release_unfinished(system)` | no; disabled since 2026-10-25 (`cloud-local-shutdown.json`) |
| Task QA rerun controllers (`oq6-tq-gate-rerun-*/controller.py`) and their `omp -p` workers | the Task QA lane | controller PID and its result directory under `~/Developer/eval-lab-results/<date>/` | SIGTERM to the controller | yes, lab job, after triage escalation |
| `queue_keeper.py` | Research or Night Planner | `_queue.log` (`dispatch`, `outcome`, `hold`, `queued … from queue-append`), `queue-state.json` | none by us | no, report only |
| OMP lanes and Peter's chats (herdr panes) | Peter or Research | session JSONL in `~/.omp/agent/sessions/` (`message.usage`, `stopReason`, `model_change.resolvedModelIsFallback`, tool calls); `herdr pane list` `agent_status`; `omp usage` | never | **no**: report, and append a fix item to `queue-append.jsonl` for the lane or the Night Planner (`wK:p1J`) |

Problems already visible during the inventory, which the first shadow pass should catch:

- `sync-primary` has logged `Skipped: working tree dirty (2 files)` every 5 minutes since
  10-28 3:03 AM UTC. The primary checkout is 543 commits behind `origin/main`.
- `run-atlas-watch` ends every cycle with `DailyTokenBudgetExceeded` (`written: 0`,
  1,990 pending), and `docent-watch` with `hosted_free_allowance_exhausted` (778 pending).
  Both are capped and spend nothing; they are stuck no-ops.
- 44 `ts14-native-repairs-*` apps and older lab apps remain `deployed` with 0 tasks.
- A local `docker compose` Harbor trial (`evallab hack run … --execute`) is running,
  although the wave contract sets `EVALLAB_DISABLE_LOCAL_DOCKER=1`.
- `omp usage`: Codex, xAI and Antigravity are at 100% of their weekly windows.

## Triggers (deterministic, $0)

Each trigger reuses an existing detector where one exists. Thresholds are fixed before scoring.

| Trigger | Harbor trial | Modal app or launchd service | OMP lane |
|---|---|---|---|
| silence | no new step or trajectory write for 15 min (`live_watch` `stalled` uses 10 min; Run Health latency alarms at a 45-min gap) | no log line or ledger update for 3 cycles | `agent_status=working` and no session event for 30 min outside a declared long tool call |
| repetition | `run_health.loop` components (8 identical commands with the same output, 10 identical messages, 5 completion claims, cycles) | the same non-ok status for 6 or more consecutive cycles | 3 identical non-`wait` tool calls, or 3 `wait` returns with no new artifact |
| burn | prompt tokens per step above the p99 for the source (TB4 p99 228k), or `live_watch` `budget_burn` | spend-ledger reservations at 80% or more of the cap | lane tokens per hour above 3× its 7-day median; a provider window rising more than 20 points in 6 h |
| wall time | elapsed above the source p99 (Goodfire 5,877 s, TB4 5,371 s), or `run_health.runaway` | app `tasks > 0` for longer than its declared timeout | one item longer than 3 h, or a blocked outcome after more than 1 h |
| error signature | `run_health.infra` catalog on the tool-output tail; `live_watch` `proxy_errors` and `infra_spike` | `ImageBuildError`, `served_route_mismatch`, sandbox not found, budget or allowance exhausted | `stopReason=error` or fallback switches 5 or more times in 10 min |
| trace-harness alarm | a new `alarm` record with `kind=cheat` in `trace-harness/findings-v1.jsonl`, or a `live_watch` integrity rule (`hidden_test_read`, `grader_tamper`, `history_mining`) | — | — |

## Decisions

A fired trigger proposes an action from a fixed rule table. The model's job is to confirm or
overrule that proposal; it never sees unflagged subjects. Run Health RH-4 found that Haiku
over-reports waste on clean runs, so it reads only after a trigger.

| Step | Reader | Input | Output |
|---|---|---|---|
| triage | `anthropic/claude-haiku-5-5` | trigger evidence and the last 20 steps or log lines, at most 8k tokens, label-free | action, confidence, cited steps, a reason of at most 40 words |
| decide | `anthropic/claude-sonnet-5-5`; `anthropic/claude-opus-5-5` when a kill would discard more than 30 min of work or paid compute | the triage output plus the full recent window | the final action |

Models are called through `run_atlas.GatewayTransport` on subscription routes only, and the
served model is attested.

| Action | Meaning | Effect in act mode |
|---|---|---|
| `continue` | slow or odd, but progressing | none |
| `nudge` | stuck but recoverable | lab job: a stop-and-retry note in its log; OMP lane: append a `queue-append.jsonl` item |
| `kill_and_retry` | agent-side stall, loop or runaway | stop the trial or app; requeue once |
| `kill_and_mark_infra` | the environment is dead | stop; record infra; do not retry the same backend within 30 min |
| `flag_cheating` | trace-harness or integrity alarm | evidence card to the Trace Watcher lane (`causal-prefix-monitor.md`); stop only if the `live_watch` integrity rule is stop-eligible |
| `alert_research` | needs a human or another lane | append to `live-supervisor.md`; never page |

## Records

Each decision is one `trace-findings/v1` record (wave-6 contract §3b):

- `detector` is `live_supervisor` and `detector_version` is `live-supervisor/1`.
- `kind` is `loop`, `runaway`, `latency`, `infra`, `cheat` or `other`.
- `verdict` is `alarm` for any action except `continue`. It is `unknown` when there is no
  actor transcript.

Extra keys, which the CE-23 importer ignores, are `action`, `mode`, `subject`
(`kind`, `id`, `owner`, `killable`), `triggers` and `acted`.

`run_id` is the trial's store trace id when it has one, and `live:<kind>:<id>` otherwise.
The importer accepts only admitted DEV runs, so the live log is
`~/.local/state/evallab-prod-live/live-supervisor/decisions-v1.jsonl`. Replay records for
DEV runs can also be imported with `evallab prod import-trace-findings`.

## Safety

- Never kill, restart or message Peter's chats or any persistent tab. Fleet problems become
  reports and `queue-append.jsonl` items.
- Act only on lab-started jobs. That means a Harbor trial under a lab job directory, a Modal
  app on the name allow-list, or a controller PID started from an eval-lab worktree. Every
  action logs its reason.
- Caps:
  - one kill per subject;
  - three kills per hour and ten per day across the lab;
  - no kill on a trial that has started verification.
- Shadow first. Replay on labelled history (G6 stuck loops 36/16, Harbor timeouts 129/347,
  seeker infra 29/27, dead sandboxes 5, CheatBench DEV 75/75, Goodfire CM-6 80/70) and run
  live. Enable an action only after its precision is 0.9 or better on adjudicated cases.
