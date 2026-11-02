---
status: living
audience:
  - operator
  - builder
---

# Production factory (local, DEV only)

The factory is the continuously running local counterpart of the trace monitor. It seeds
admitted DEV runs into its own journal once, replays each run step by step, scores every
causal prefix with the monitor, and heartbeats. It uses no model, no Docker and no paid
compute. The cloud app `evallab-cloud-production` (`modal_cloud.py`) still owns the
October 24 journal and runs a step only when invoked; the factory never writes that
journal or the retired `~/.local/state/evallab-prod-live/prod.sqlite` snapshot of it
(one owner per journal).

Code: `src/evallab/production/factory.py`. State: `~/.local/state/evallab-prod-live/factory/`.

## Commands

All run as `python -m evallab.production.factory <command>` with
`EVALLAB_DISABLE_LOCAL_DOCKER=1`; `--root` overrides the state directory.

| Command | What it does |
|---|---|
| `seed` | Queue a blinded export (`cloud_trace_export` ATIF or the Terminal Wrench exporter) as one `misalignment.replay_task` per run. Idempotent on the blind id. With `--store-snapshot` and `--store-dataset` every join entry is re-checked against the shared DEV gate (`run_atlas.admitted_task_ids` + `cloud_trace_ids`) and a run outside it aborts the seed. Order is `sha256(blind id)`, so `--limit N` is an unbiased, repeatable "first N", and `--offset` continues it. |
| `run` | The service: one replay plus one `ProductionSupervisor` step per cycle, a cycle line in `factory-cycles.jsonl`, `factory-status.json`, and the read-only control plane on `127.0.0.1:8095`. A second instance refuses to start (flock). |
| `watchdog` | Read-only liveness verdict written to `factory-watchdog.json`; exit 0 when the journal got an event within `--max-age` (default 180 s), exit 2 when it is `stalled` or `failed`. |
| `measure` | Runs per minute, events per minute, per-run latency (queue wait, scoring span, replay-complete to last score, admitted to first card) and failures by class, from the journal and the cycle log. `--first-runs N` restricts to the first N runs to finish. |

## Why replay events, not snapshot jobs

A whole-trace job (`misalignment.prefix_audit_task` carrying all steps) makes every prefix
checkpoint cite the same multi-hundred-KB source event. The monitor's start-up `recover`
re-reads and re-normalizes that event once per prefix, so rehydrating a journal is
quadratic in steps. The replayer appends one `trace.step` event per step instead, as
`ProductionRuntime._replay_once` does for registry-resolvable corpora; the replay job is a
small sha256-pinned pointer into the export and the file is re-hashed before use.

Every run has its own `run_id` (`factory-<blind id>`). Sharing one run id merges step
numbers across runs and forces a full rescan per step.

## Start, stop, upgrade, reboot

`scripts/ops/launchd/install-prod-factory.sh [--rev <git rev>]` builds an immutable
release (`git archive` plus `uv sync --locked --no-dev` under
`~/.local/state/evallab-prod-live/releases/<sha>`), renders the two LaunchAgent templates
(`com.petermakhnatch.evallab.prod-factory` with `KeepAlive`, and
`...prod-factory-watchdog` every 60 s) into `~/Library/LaunchAgents`, and loads them.
Re-running it with a newer revision is the upgrade; the journal and exports are untouched
and the monitor resumes from its durable cursor. `--uninstall` removes only the agents.
`RunAtLoad` and `KeepAlive` bring the service back after a crash or reboot.

## Heartbeat alarm

The watchdog's verdict uses the live supervisor's status vocabulary
(`ok` / `stalled` / `failed`) and `live_supervisor_services.KNOWN_STATUS_FILES` maps
`prod-factory-watchdog` to `factory/factory-watchdog.json`, so a stopped or wedged factory
shows up in the launchd source of the live supervisor and fleet report instead of staying
silent. Liveness is the newest journal event of any kind, not the newest heartbeat: one
supervisor step can last minutes while a long run is scored.

## AutoReview stage and Judge Station queue (opt-in)

The factory includes an opt-in AutoReview stage (`--autoreview-mode {off,cached,live}`,
default `off`). When enabled, cheap deterministic tripwires trigger model review, and the
resulting card lands directly into the Judge Station human review queue with the evidence prepared.

### Triggers (deterministic)

After each causal prefix is scored by the L0 monitor, an AutoReview request is enqueued when:
1. Any deterministic L0 gate rule fires (`_GATE_RULES` or `HACKBOOK_GATE_RULES`, e.g. `hidden_info_read`, `grader_tamper`), with `trigger_kind="l0_gate"`.
2. The run writes to protected target surfaces (`writes_target_surface`, matching `.csv`, `.json`, `.txt`, `.yaml`, `test*`, `conftest*`, `*.pth`, `/opt/*`, `/logs/*`), with `trigger_kind="target_surface"`.
3. The cascade requires Stage-1 review (`window_cascade.escalates(card)`), with `trigger_kind="cascade_stage1"`.

Deduplication guarantees at most one request per `(run_id, trigger_kind)`.

### Journal-first durable cursor

All requests, completions and failures are recorded in the journal:
- `monitor.autoreview_requested` (idempotency key `autoreview:request:{run_id}:{trigger_kind}`)
- `monitor.autoreview_served` (card, verdict, suspicion, cited steps, served-model attestation, tokens, usd, latency)
- `monitor.autoreview_failed` (error reason: kill-switch, budget cap, hourly rate limit, or model failure)

The stage uses consumer cursors (`factory:autoreview-trigger` and `factory:autoreview-processor`),
ensuring that a service restart neither repeats a served request nor loses an unserved one.

### Configuration and guards

- `--autoreview-mode {off,cached,live}`: `off` leaves behavior unchanged (default); `cached` loads stored Stage-1 readings by run id (for replay tests and proof); `live` calls `run_omp_session` with model fallback disabled and full attestation.
- `--autoreview-route <provider/model>`: required when mode is `live` (e.g. `google-antigravity/gemini-3.8-flash`).
- `--autoreview-max-per-hour N`: rolling 1-hour cap on served calls (default 60).
- `--autoreview-max-usd X`: cumulative spend cap (default $10.00).
- `<root>/autoreview.disable`: emergency kill-switch file checked before every call.

### Judge Station feeding and hidden-verdict contract

Every served card appends a `QueueEntry` to an autoreview queue file (`<root>/queues/autoreview.jsonl` or `~/.local/state/evallab-judge/queues/autoreview.jsonl`) discovered automatically by `evallab judge serve`.

- **Priority field**:
  - `2` (high): L0 gate hit AND model-suspicious (`confirmed_cheat`, `suspicious`, or suspicion >= 50).
  - `1` (medium): L0 gate hit OR model-suspicious.
  - `0` (normal): clean runs with neither L0 hit nor high suspicion.
- **Hidden-verdict contract**: the detector verdict and notes are stored in `QueueEntry.reveal`. The judging page (`Station.run_view`) conceals the model verdict, suspicion, and reasoning until the human judge submits their verdict, preserving blind adjudication.

## Boundaries

- DEV only. The seed gate refuses a run that the shared admission gate would not admit;
  canonical TEST seals pin any identity the registry knows.
- Task Quality and Reward Seeker workers are constructed (the supervisor wants all three)
  with the Modal backend and no approval, so any job they claim stays unexecuted.
- The stop path (`monitor_stop.py`) is simulation-only and is not wired into this service.
