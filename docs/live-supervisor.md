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
  1,990 pending), and `docent-watch` with `hosted_free_allowance_exhausted`. Both are
  capped and spend nothing; they are stuck no-ops. A denied `docent-watch` tick now
  reports `inventory: "not_measured"` and omits `eligible`/`completed`/`pending`: it
  checks the allowance before admission, and measured ticks report `inventory: "measured"`.
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
Source benchmark metadata is withheld from reader metrics and trigger descriptions;
the source-specific threshold still applies.

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

Cheating alarms produce evidence cards and one queue notification per UTC day. Routing
holds a shared file lock around card, index, notification and deduplication-state writes,
so the live subscriber and a scan actuator cannot race to send duplicate notices.

## Running it

`evallab live-supervisor` is the operator entry point. Everything it runs is **shadow mode**:
decisions land in `<root>/decisions-v1.jsonl` and nothing is killed, stopped, restarted or
messaged. `act` mode is not a flag here yet; LS-4 adds it with the caps below.

```bash
# one cycle over every source, rules decider ($0), human-readable lines
evallab live-supervisor scan --reader rules --root /tmp/ls-smoke

# one cycle as one JSON object, for a log or a jq query
evallab live-supervisor scan --source harbor --source services --json

# the model reader: Haiku triage, Sonnet or Opus to confirm or overrule; capped per UTC day
evallab live-supervisor scan --reader model --max-tokens-per-day 500000

# a foreground loop (development only; launchd runs scan every 120 s instead)
evallab live-supervisor serve --interval 120
```

- `--source` is repeatable and defaults to every source: `harbor` (live trials under the run
  roots), `services` (launchd jobs), `modal` (apps), `processes` (`ps`), `fleet` (OMP panes,
  the queue keeper, provider quotas). Sources are imported by name on use, so registration and
  `--help` open nothing. A source that fails is reported under `errors` and the others still run.
- `--reader rules` accepts the trigger table's proposal unchanged. `--reader model` refuses to
  start without `--max-tokens-per-day`: no unattended run spends tokens uncapped. It also
  checks reader headroom once per cycle through `run_atlas_watch.routes_with_headroom` (the
  `omp usage` contract, 90% window rule) and sends nothing while a reader route is used up.
- `--root` defaults to `~/.local/state/evallab-prod-live/live-supervisor` and holds
  `decisions-v1.jsonl`, `state.json` (the 30-minute per-subject cooldown), `fleet-state.json`
  and `supervisor.lock`.
- `scan` and `serve` take an exclusive, non-blocking flock on `<root>/supervisor.lock`. A second
  process exits with `another live-supervisor process holds …` instead of interleaving writes;
  the lock file is never unlinked, so two processes cannot agree on different inodes.
- `serve` prints one JSON object per cycle and keeps the sources and the model gateway for the
  process lifetime. Ctrl-C stops it with exit 0.

`replay` and `adjudicate` forward their command line to the sibling tools
(`live_supervisor_replay.main`, `live_supervisor_adjudicate.main`), so flags are documented
there; put anything this command does not declare after `--`:

```bash
evallab live-supervisor replay --out /tmp/replay-rows.jsonl --set loop --set infra -- --workers 4
evallab live-supervisor adjudicate --rows /tmp/replay-rows.jsonl --windows /tmp/windows.jsonl \
  --out /tmp/adjudicated.jsonl --limit 120
```

### Failure policy

- A reader that fails, returns unusable output, has exhausted its daily token budget, or has a
  reader route at or above the 90% window never produces an unconfirmed kill: the decision
  becomes `alert_research` and the record's `reason` says which reader or provider was
  unavailable.
- A subject with no actor transcript is never `clear`: its verdict is `unknown`.
- One subject's trigger or actuator error is recorded against that subject and the cycle
  carries on with the others; one failing source is reported in `errors` and skipped.
- A process that cannot take `<root>/supervisor.lock` exits without writing, so two cycles
  never interleave decisions.

### Routing cheat alarms

```bash
# one pass: route new cheat alarms to the Trace Watcher (cards + at most one queue line a day)
evallab live-supervisor route-alarms --once

# a scratch run: no bridge queue, no real state, history included
evallab live-supervisor route-alarms --once --root /tmp/ls-smoke --queue /tmp/ls-smoke/queue.jsonl \
  --replay-existing
```

`route-alarms` tails the trace-harness findings (plus the newest Run Health findings file) with
`live_supervisor_cheat.subscribe_alarms`, keeps only `verdict=alarm` `kind=cheat` records — the
subscriber already skips the supervisor's own records — and hands each new run to
`CheatRouter.route`: one card under `<root>/cheat-cards/` with a real step excerpt, one
`queue-append.jsonl` line for the Trace Watcher pane per UTC day. It writes no document, pages
nobody and cannot stop a run.

- The first start begins at **EOF**: the existing findings are consumed without routing, so the
  Trace Watcher is not flooded with history. `--replay-existing` routes the history instead.
- The cursor `<root>/alarm-cursor.json` is read into `alarm-cursor.candidate.json` and replaces
  the durable file only after the router returns. A crash replays the same records, and the
  router's per-run dedupe prevents a second card for a run it already carded.
- The observation's events come from the live trial (`agent/trajectory.json` under a
  `--trial-root`, default the Harbor run roots) through `trial_observation`; the detector rows
  go in `metrics.trace_alarms`. A run with no local trial has no events, the alarm row itself is
  the evidence, and the card says to open the run. Steps are never invented.
- `--interval` loops the pass in the foreground; Ctrl-C stops it with exit 0.

### launchd

`scripts/ops/launchd/com.petermakhnatch.evallab.live-supervisor.plist` runs one `scan` every
120 s with `--reader model --max-tokens-per-day 500000`, and
`…live-supervisor-alarms.plist` runs `route-alarms --once` on the same interval. Both are
templates: render `__EVALLAB__` (the release's `venv/bin/evallab`), `__WORKDIR__` (that
release), `__HOME__` (for `omp` on `PATH`) and `__LOG_DIR__` before installing, and do not load
an unrendered file. Installation is explicit and out of scope for this document; neither job is
loaded by merging this change.

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

### Actuator (LS-4)

`production/live_supervisor_act.py` is the only thing that acts, and it is off by default:

- `act_actions` names the actions an operator approved; empty means everything is report-only.
  Kills also need an operator registration of the immutable identity (`app_id`, or `job/trial`)
  in `registrations-v1.json`, written with `register_app` / `register_trial`. A lab-looking
  name, prefix or path is never enough, and a job directory must resolve inside an explicit
  lab runs root.
- Every effectful action needs `production/live_supervisor_gate.py` to pass:
  `gate_from_adjudication(root, headline.json)` publishes LS-3's adjudicated counts as
  `gate-evidence-v1.json` plus a pinned SHA256. The gate refuses when the file is missing,
  unpinned or changed, when no scope matches the action + detector version + subject kind +
  reader model (+ trigger when the scope names one), when fewer than **20** adjudicated cases
  back the scope, or when `TP/(TP+FP) < 0.9`. Sources are never pooled: an action passes only
  if every measured source passed it.
- The LS-3 results as of 2026-10-28 (opus-v1) pass no action: best is model `kill_and_retry`
  6/7 = 0.857 (n=7, also below the 20-case floor), and rules `kill_and_mark_infra` was 0/15.
  So the gate refuses every action and the supervisor stays shadow.
- `kill_and_retry` is also blocked until a real retry admission is injected (an admission into
  the lab queue). A kill whose admission fails is receipted `partial` and the ledger row says
  `retry_pending`, never `retried`; `kill_and_mark_infra` has no retry and is unaffected.
- The rules `kill_and_mark_infra` arm additionally needs a *fallback-succeeded* check (did the
  retry on another backend actually finish?) before its precision can be trusted; that check is
  not implemented, so the arm stays refused on its measured precision alone.
- Caps: one kill per subject ever, three per rolling hour, ten per UTC day; every real attempt
  counts, and the reservation is persisted before the external stop runs. Every attempt appends
  a receipt to `actions-v1.jsonl`.
