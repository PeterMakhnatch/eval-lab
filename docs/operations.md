---
status: living
audience:
  - runner
  - operator
---

# Operations

The queue, native runners, and evidence stores remain the operating surface.
The continuous operator, Fleet supervisor, and GEPA engines are retired.
Their historical journals and campaign receipts remain evidence, not runnable instructions.

## Local database

Copy `.env.example` to `.env`, change the password if the database is exposed
beyond localhost, and start PostgreSQL:

```bash
docker compose up -d postgres
uv run evallab db init
```

`docker compose down` stops PostgreSQL and preserves the named volume. Deleting
the volume is intentionally not part of a Make target.

## Standing-policy queue

The committed policy in `policy/standing-approvals.yaml` says which **free**
work the unattended executor may run on its own; billable work is never
covered by it (see "Paid execution requires a recorded authorisation" below).
Only Peter changes that file. Agents submit a Pydantic-validated experiment
spec; the policy gate moves it atomically to `approved/` or `waiting/` and
records every transition in ignored local `queue/events.jsonl`.

Example control spec:

```json
{
  "schema_version": 1,
  "name": "event-summary-oracle-queue-1",
  "hypothesis": "The reference solution remains accepted.",
  "task": "library/tasks/event-summary",
  "agent": "oracle",
  "submitted_by": "peter",
  "priority": 100,
  "est_cost_usd": 0
}
```

Submit and drain:

```bash
uv run evallab submit /path/to/spec.json
uv run evallab tick
```

`submit` prints the `spec_id` on its own labelled line. That bare ULID is the
value `approve` and the catalog's `experiment_id` column take;
the `state:` and `path:` lines beside it are context, not identifiers.

Out-of-policy work waits for an explicit decision:

```bash
uv run evallab approve <spec-id> --actor peter
```

An approval does not override the hard per-job or daily cost ceiling. A restart reconciles
`queue/running/` against completed immutable Harbor jobs before starting new
work. Self-hosted-model specs probe the upstream first: a cold endpoint waits
up to `--selfhosted-warmup-seconds` (default 600) for a 200 and otherwise
defers the spec approved. Daytona batches are clamped up front to the memory
cap, and multi-model batches run one smoke trial first unless passed
`--no-smoke-gate` (recorded in `queue/events.jsonl`).
The cost-policy day is UTC: both catalog spend and durable attempt reservations cross the daily
ceiling boundary at 00:00 UTC, regardless of the host or PostgreSQL session
timezone. The researcher call ledger uses that same UTC budget day before it
combines attributed researcher reservations with catalog spend, and resamples
the clock before every role attempt and retry so a long pass cannot carry the
prior day's budget across midnight.

The legacy `run` and `matrix` commands are restricted to Oracle/no-op controls.
All real-model work must pass through the queue and standing policy.

## Paid execution requires a recorded authorisation

**Nothing billable runs unless Peter authorises that specific spec.** A spec is
billable when its agent is anything other than `oracle` or `nop`. Paid work in
this lab is dispatched against Peter's ChatGPT subscription
(`~/.codex/auth.json`, `src/evallab/runner.py`), so no dollar ceiling in this
repository measures what it actually consumes; the human decision is the only
control that does.

Which classes need authorisation:

| Class | Agents | Runs unattended? |
|---|---|---|
| `local-controls` | `oracle`, `nop` | yes — no authorisation, no ceremony |
| everything else | `codex`, `claude-code`, any future paid adapter | no — one recorded authorisation per spec |

For the DeepSeek credential contract and native runner configuration, see [`docs/third-party.md`](third-party.md).

How it is enforced, in order:

1. `PolicyGate.decide` refuses a billable spec that carries no authorisation
   with `paid_run_unauthorized`, **before** any `auto_run` rule is consulted.
2. `standing_rule_admits` returns `False` for every billable spec, so a
   standing rule cannot cover paid work even if one names a paid agent. Editing
   `policy/standing-approvals.yaml` therefore cannot re-open unattended spend.
3. The authorisation itself is the `human_approved` record in append-only
   `queue/events.jsonl`, written by `evallab approve`; the automation that
   submits paid work does not write that ledger entry.

It fails closed. If `queue/events.jsonl` cannot be read, the whole tick stops
with `authorization_ledger_unreadable` and dispatches nothing, free controls
included. An authorisation recorded before its spec was submitted is
`paid_run_authorization_stale`, so reusing a spec id does not inherit consent.
An authorisation naming another spec is `paid_run_authorization_mismatch`.

### Authorising a paid run

```bash
uv run evallab submit queue/specs/my-codex-run.json
#   -> state: waiting, reason paid_run_unauthorized, with both commands printed
uv run evallab approve <spec-id> --actor peter
#   -> prints the agent, attempt count, estimated cost, and the quota state
uv run evallab tick
```

`--actor` is required on `approve`; it is never defaulted, because the recorded
name is the whole point of the record.

Authorisation does not lift a ceiling, the quiet-failure breaker, the registry
checks, or the credential requirement — those still refuse an authorised spec.

## What the quota gate does and does not decide

Authorising a paid run used to say nothing about what it would cost, because
the only figure available was a dollar equivalent that never moves. The
measurement in `src/evallab/quota.py` closes that: the agent CLI writes the
provider's own `rate_limits` block into every trial's session rollout, Harbor
copies it out, and the gate reads it. No network call, no credential store, no
API key.

Every billable decision — the `paid_run_unauthorized` refusal at `submit`, the
`approve` output, and the admission at `tick` — now carries `used_percent`,
`remaining_percent`, `resets_at`, `hard_stop`, the age of the reading, and the
rollout it came from. The scope line is load-bearing: the percentage is
**account-wide**, and the lab's share of it is structurally unknowable, so
"8% remaining" is not "8% remaining for the lab".

### Quota refusals, and back-off on a rate limit

`subscription_quota_exhausted` refuses a billable dispatch when the provider
reports `rate_limit_reached_type`, or `used_percent` at or above 100. That is
the provider's own account of its allowance, not a threshold this lab invented,
which is why it needs no judgement call. With `hard_stop` true — no overflow
credits, balance `"0"` — reaching 100% is a lockout for every paid agent until
the window resets, not an extra charge.

`subscription_quota_ceiling` refuses a billable dispatch when the lab's own
threshold is reached: `refuse_billable_at_used_percent` in
`policy/standing-approvals.yaml` governs the ceiling, currently committed at
**80%**, raised from the original **60%** baseline introduced for the 2026-10-13
overnight subscription-agent work on the shared account. The two codes stay
distinct so a lab policy is never recorded in `queue/reasons/` as the provider's
statement. Lower the number to tighten; raising it or returning it to `null` is
Peter's spend decision.

Back-off on a rate-limit response is explicit: stop launching billable work,
record the provider-stated `resets_at` from the refusal message, and resume
only after it. Never switch the job to an API to route around the window.

Claude Code uses a fresh, free `omp usage -p anthropic --json -r` reading at
dispatch rather than relying on its transcripts. The committed ceiling applies
to its **7-day** window (currently at or above 80%); its **5-hour** window
gates only at 100% exhaustion. Both windows, reset times, status, and fetch
time are recorded.
`subscription_quota_unavailable` pauses Claude dispatch when either window is
missing, malformed, or expired, including a failed or timed-out probe. The
historical Codex bootstrap and stale-reading behavior described below applies
to artifact-based Codex readings; Claude's free live probe needs no paid call
to recover. See [quota measurement](#quota-measurement-and-portable-sidecars)
for dispatch sidecars and deterministic tests. `--despite-quota` does not
override `subscription_quota_unavailable`.

### Unavailable is not "plenty left"

A checkout with no paid Codex trials has no Codex reading. The gate reports that as
`UNKNOWN [unavailable]` with its reason, and never as a number. It refuses
nothing on an unavailable reading — refusing would be a bootstrap deadlock,
because only a paid trial can produce a reading — but it never permits
silently either: the UNKNOWN banner reaches the named human who is being asked
to authorise, and unattended automation cannot authorise paid work at all.
A reader that throws is also `unavailable`, with the exception in the reason.

### Stale readings warn; they never refuse

The artifact-based Codex reading is only as fresh as the last paid trial. The recorded reading was
5h18m old. Both failure modes are real, and they point in opposite directions:

- **Refusing on age deadlocks the lab.** Freshness is produced *by* paid runs,
  so after any quiet period every reading is stale and the first paid run
  becomes impossible — permanently, not until some timer expires.
- **Trusting a stale reading silently is how a lockout arrives unannounced.**
  This repository has measured the drift: the historical quota receipt records
  the account moving 71% → 91% in roughly five hours of ordinary interactive
  use, none of it the lab's. A five-hour-old figure has a demonstrated capacity
  to be twenty points wrong.

The resolution is that the gate is not the right actor to decide. It cannot
make a stale reading fresh, and a refusal would not have prevented that drift.
What it can do is refuse to present an old number as a current one: the age is
printed next to every figure, in the same block, always. The judgement stays
with the named human that #65 already requires — who can, unlike this gate,
look at the provider directly.

One consequence needs its own escape hatch. A reading that says *exhausted* can
itself be stale, and a paid trial that recorded 100% just before the window
rolled over would otherwise lock the lab out forever. Two things prevent that:
a reading whose `resets_at` has already passed describes a window that no
longer exists and refuses nothing; and

```bash
uv run evallab approve <spec-id> --actor peter --despite-quota
```

records the override on the `human_approved` event as
`reason_code: quota_override`. It lives in the event log, not the spec file,
for the same reason the authorisation does — the automation writes the spec
file. It overrides `subscription_quota_exhausted` and
`subscription_quota_ceiling` and **nothing else**: every ceiling, the
quiet-failure breaker, and all four of #65's fail-closed states still fire
against it.

Free `oracle` and `nop` controls spend no allowance and are untouched by all of
this — no quota is read on their behalf and no quota state can hold them.

## Preflight: is it safe and sensible to run right now

```bash
uv run evallab preflight
```

One read-only surface answering the question you actually have before starting
work, from evidence already on disk. It costs nothing and blocks on nothing: no
paid call, no provider request, no `gh`, no database, no subprocess, no socket.
`tests/test_preflight.py` proves that by making `subprocess` and `socket` raise
and rendering anyway. It exits `1` when a provider's own reading refuses
billable work, so `evallab preflight && evallab tick` stops before it spends.

It prints three things.

**Remaining quota, per provider.** `codex` and `claude-code` are two separate
subscriptions, so they get two separate readings built from two separate scans
— one account's headroom is never attributed to the other. Each reading carries
`used_percent`, `remaining_percent`, `resets_at`, `credits_balance`,
`hard_stop`, the age of the observation, and the rollout it came from, with
`[observed]` / `[unavailable]` exactly as `approve` prints them. All figures in
a block come from one snapshot, so the block carries one `staleness` line
rather than repeating the same age on each row. A provider with no reading
prints `UNKNOWN [unavailable]`, its reason, and the sentence saying UNKNOWN is
not "plenty left" — never a number, and never a blank.

**The queue, grouped by purpose,** over the unfinished states (`proposed`,
`pending`, `approved`, `waiting`, `running`). Finished states are history: they
cannot spend and cannot be under-powered. A spec file this build cannot validate
is listed by name with its error — under-reporting the queue is the one thing this surface must
not do. It reads the state directories directly instead of through
`DirectoryQueue`, which creates them, because reporting that there is no queue
must not create one.

**Power warnings, for queued comparisons only.** Computed with
`cohort.minimum_detectable_effect`, the estimator used in every comparison report.
With no comparison queued it says so; it does not
manufacture a warning. It warns when a comparison cannot reach a task-paired
interval at all — fewer than two distinct tasks, or no detectable per-attempt
difference at the declared `attempts` — and when no arm declares an
`expected_reward`, because then the detectable effect cannot be computed
*before* the money is spent.

Two honesty limits are printed with the numbers rather than left implicit. The
queue carries no field linking two specs into one comparison, so every queued
comparison spec is pooled into a single cohort; pooling can only overstate
`n_tasks`, so a warning at the pooled `n` holds for any finer partition, while
a clean bill of health at the pooled `n` does not. And no threshold for
"useful" is committed — a spend judgement, like the quota ceiling was before
its original 60% baseline (now raised to 80% in
`policy/standing-approvals.yaml`). Supply one per invocation:

```bash
uv run evallab preflight --useful-effect 0.15
```

and a comparison whose smallest detectable difference exceeds it becomes a
warning.

The operator-facing `evallab tick` prints the same block before it builds an executor.

## CORE tool health

Run the existing doctor from an isolated, locked checkout:

```bash
uv run --locked evallab doctor
```

The command exits 1 when any required local or CORE boundary fails.
It reports installed versions against the supported pins in `uv.lock`, `runner.py`, and `automation.py`.
The inventory in [`third-party.md`](third-party.md) names each owner, contract test, failure mode, and recovery path.
The command makes no model call, data download, container launch, or installed-service change.
GitHub auth and protection visibility use free, read-only metadata requests.
An offline host reports that boundary unavailable.

The checks include:

- Harbor 0.24.0 in the normal runtime, Docker Engine, Compose, and the shared Linux-daemon isolation preflight.
- PostgreSQL connectivity, schema fingerprints, the missing applied-schema ledger, and tools-1 backup freshness.
- SQLite read-only opens and format versions, DuckDB compatibility, and every retained Parquet footer.
- OMP CLI version and config resolution of `retry.modelFallback: false`, with a true control overlay.
- The pinned keys executable hash and `keys has HF_TOKEN`, with both credential-probe streams discarded.
- GitHub auth, rulesets or classic branch protection, and visibility of both required CI checks.
- uv, supported Python versions, and locked client/parser versions.
- Goodfire and Hugging Face revisions, native file hashes, and normalized bundle hashes, without a download.
- Goodfire action metadata through `evallab.gold.load_rows` only; this check changes no labels or registry.
- Every installed evallab LaunchAgent's source revision, loaded state, last exit, and useful-output age.

Doctor reads installed plists, not repository templates, and uses only `launchctl print`.
Useful-output limits are 3 schedule intervals, at least 15 minutes, or 36 hours without a fixed interval.
A recent stdout log or idle factory cycle does not refresh useful-output age.
A running job can have no previous exit code; an unloaded job or a known nonzero exit still fails.
Missing pins, missing useful-output receipts, and stale useful output remain visible failures.
The daemon preflight does not replace native per-container mount and network proof.
The OMP config check does not prove provider availability or authorize a model call.
Large retained stores can take minutes because doctor verifies source hashes and Parquet footers.

## Headless readiness and scheduling

The scheduler fails closed. This command prints a versioned JSON object whose
runtime fields are booleans only:

```bash
uv run evallab doctor --headless
```

It checks that the Claude OAuth Keychain item is readable without a GUI prompt,
Codex's `~/.codex/auth.json` exists, Docker is reachable, PostgreSQL responds,
and at least the configured disk headroom remains. It never prints or stores a
credential value. Infrastructure determines overall readiness; credential
booleans determine which model-agent specs can run. Oracle/no-op controls remain
runnable with neither model credential, and the executor defers only a spec
whose own credential is absent. The default Keychain service remains
`harbor-practice-claude-oauth` until brief 11 migrates the auth scripts; override
only the service/account names with `HARBOR_CLAUDE_KEYCHAIN_SERVICE` and
`HARBOR_CLAUDE_KEYCHAIN_ACCOUNT`.

It also audits the installed schedule. Every
`~/Library/LaunchAgents/com.petermakhnatch.evallab.*.plist` is parsed and the
paths it must resolve to start are checked: `Program`, the runtime an exec
wrapper names (a shell `-lc` command word or `keys run -- <interpreter>`), the
`WorkingDirectory`, and a shell command's leading `cd` target. A plist this
build cannot parse counts as broken for its own filename label. Each broken
agent is named with its missing path — `evallab doctor` prints one
`launchagent` line per problem, for example
`FAIL  launchagent    com.petermakhnatch.evallab.tick: interpreter /path/python missing`,
and the headless report carries `launch_agents.broken` entries of
`{label, plist_path, kind, path}` plus `checks.launch_agents_intact`. The audit
is read-only: the headless path reads plists without calling `launchctl`.
The normal doctor also reads loaded state with `launchctl print`; neither path changes an agent. `healthy` stays
infrastructure-only so one stale agent cannot quarantine an in-flight cycle;
the `ready` field and the `doctor --headless` exit code require both healthy
infrastructure and every installed evallab LaunchAgent resolvable.

`evallab doctor` also prints one advisory `sync-primary` row for the primary
checkout's sync timer (`scripts/sync-primary-checkout.sh`, installed by
`scripts/ops/launchd/install-sync-primary.sh`). The timer fetches and
fast-forwards one checkout, never resetting or discarding local work, and
records why each run synced or skipped in
`~/Library/Application Support/evallab/sync-status.json`
(`EVALLAB_SYNC_STATUS` overrides the path; `--state-dir` at install time moves
it). The row names the last run's outcome, its reason, how many commits HEAD is
behind the remote ref, and how old that run is:

```
ok    sync-primary   up-to-date (up-to-date) behind 0 age 42s
FAIL  sync-primary   skipped (dirty) behind 848 age 3m; skipped while 848 commits behind
```

- `ok` — the timer ran within three intervals and either fast-forwarded or had
  nothing to do.
- `FAIL ... stale: last run <age> ago (limit 900s)` — no run for more than three
  timer intervals (default 300s, so 15 minutes; `EVALLAB_SYNC_INTERVAL_SECONDS`
  overrides it). The agent is not running, was unloaded, or its script fails
  before writing status. Check `launchctl print gui/$(id -u)/com.petermakhnatch.evallab.sync-primary`
  and `~/Library/Logs/evallab/`.
- `FAIL ... skipped while N commits behind` — the timer ran but refused to move
  the checkout: usually a dirty working tree (`reason dirty`, with the file
  count in the status file), an off-branch HEAD, a diverged branch, or an
  in-progress merge/rebase. Clean, commit or rebase that checkout; the timer
  never does.
- `FAIL ... missing <path>` — the timer has never written status here, so the
  primary is not being synced at all. Install it with
  `scripts/ops/launchd/install-sync-primary.sh --load`.
- `FAIL ... unreadable <path>` or `fetch failed` — the status file could not be
  parsed, or the last fetch failed, so the run says nothing about freshness.

The row is advisory: `sync-primary` is not in doctor's `required` set, so it
never changes doctor's exit code or the headless `ready` field.

- `com.petermakhnatch.evallab.nightly` runs the **$0 refresh** at 02:30 ET: `nightly --refresh`, never queue dispatch or the research cycle. For the stable installed runtime (no tick job), use `scripts/ops/launchd/install-nightly-refresh.sh --load`; on demand use `launchctl kickstart gui/$(id -u)/com.petermakhnatch.evallab.nightly`. State, Parquet, HAR-177 ledger/verdicts, HAR-176 pages and HAR-184 replay live in `~/.local/state/evallab-nightly`; `--queue-root` selects read-only checkouts (default primary plus worktrees with queues). Unchanged inputs are a locked high-water no-op. The ten-line digest is `~/.local/state/daily-report/inputs/evallab-nightly.json`, consumed additively into `facts.json`/`facts.md` only while ≤36 hours old; installation does not trigger the reporter or any LLM. `install-nightly-refresh.sh --uninstall` boots the label out and removes the plist, keeping the runtime snapshot and refresh state; re-running it is a no-op.
  Set `--verdict-root <checkout>` when the primary lacks the real HAR-177 census/variant/oracle files (on this host: `.worktrees/har177-verdicts-20261006`). The service records per-file SHA-256/source provenance and snapshots only those inputs under `state/evidence`; retiring that source checkout does not lose the reviewed evidence. A present source missing required inputs fails closed.
  Per-job projection refusals retain the original guard and exact reason in `state.json` and the digest JSON; the ten-line summary lists reason classes. Refused jobs are excluded from run/trial/legit/history/page counts and never marked processed. Only changed source inputs retry them; unchanged input is a no-op even when refusals remain.
  Source `watch/hooks.jsonl` changes refresh the run and replay; generated `watch` and `processed` outputs do not trigger another refresh.
  Each queue root is an independent source. A configured `--queue-root` that is missing, or a root whose evidence vanishes mid-run (a worktree retired while the refresh walks it), is recorded under `source_failures` as `{path, reason}` in the result and digest JSON and on the digest's queue-roots line; the run restarts without that root and the remaining sources still refresh (`status: partial`). A missing path outside every source still fails the whole run. An auto-discovered worktree retired *before* the run is simply no longer discovered.
  Every result carries `backup`: the newest checksum-verified dump under the primary's `backups/postgres/`, its creation time, age, and freshness. The limit is 36 hours. A missing or stale backup automatically runs a dump with a 120-second guard, including on unchanged evidence. `--backup` additionally requests a dated dump while the previous dump remains fresh. The result records `attempt: {ok, path|error}`. The digest shows PASS or FAIL, the backup age, and the limit.
  Exit status: `0` means all sources and backup health pass (also `status: locked`, when another refresh holds the lock). `1` means a source fails, a backup attempt fails, or the verified backup remains missing or stale. This applies to a `noop` result too. `2` means the whole refresh fails.
  Unchanged evidence does not replay projections or advance their high-water state. If backup health or source failures changed, the digest health lines are still republished so a successful backup or retired-source recovery cannot leave yesterday's failure visible.

```bash
uv run evallab tick --canary-suite policy/canary-suite.yaml
```

The suite's `interval_seconds` controls enrollment slots, independently of the
scheduler's polling interval. It defaults to 86400 (daily) and accepts 1–86400
seconds. `max_cycles_per_day` defaults to 1 and cannot exceed the number of slots
in a UTC day. A zero-cost `oracle`/`nop` suite can use 3600 and 24, respectively,
for hourly controls. Billable members still require positive cost estimates;
the estimate across all members, agents, and permitted cycles must fit the
existing daily policy ceiling.

Only the current UTC slot is eligible; missed slots never produce a catch-up
burst. The daily cap counts actual enrolled cycles, so a late start does not
spend its allowance on missed slots. Names remain deterministic: slot zero
keeps `canary-<member>-<agent>-<YYYYMMDD>`, and later slots add `-cycle<N>`.
Every queue state counts for deduplication and the daily cap, including
`waiting`, `done`, and `failed`. STOP, an active producer lock, or any running
spec defers enrollment. Task digests are checked before new submissions, not
rehash-checked on idle or exhausted ticks.

Free controls use the existing matching standing-policy rule; the enqueuer
does not require a rule named `canary`. The committed paid suite still stages
only into `waiting/` until Peter authorizes each specific spec. Selecting a
suite grants no paid-run authority. Invalid enrollment quarantines the tick
without dispatch. The suite file must parse and validate before installation
changes any launchd service.

LaunchAgents invoke `/bin/zsh -lc 'cd <repo> && uv run evallab …'`. Logs live under
`~/Library/Logs/evallab/`. Reinstalling replaces and reloads the definitions.
Because these are LaunchAgents, not system daemons, they run inside the logged-in
user session where Keychain access is possible. The plist supplies a bounded
command `PATH` including `~/.local/bin`, so launchd can find `uv` without
depending on interactive shell startup files. It also captures the resolved,
non-secret `EVALLAB_DERIVED_ROOT`; reinstall the schedule after changing that
setting. Normal installation uses the intended primary workspace. If that checkout
is dirty or pinned to old code, do not reset, stash, switch, or blindly pull it to
make an installation succeed; use the release-owner adoption procedure below.

### Adopting reviewed code without moving the data workspace

The release owner may run a clean, exact merged revision from a dedicated
worktree while keeping the original workspace's queue, task definitions, policy,
`.env` catalog configuration, runs, and explicit `EVALLAB_DERIVED_ROOT`.
Create a fresh locked environment in that runtime checkout; never copy the
primary virtualenv or runtime state. The existing `run_cli(workspace=...)` entry
point separates workspace data from imported code. For example, the following
**read-only** command uses the reviewed interpreter and reports scheduler state:

```bash
cd /absolute/original/workspace
/absolute/reviewed-runtime/.venv/bin/python -I -c \
  'import sys; from pathlib import Path; from evallab.cli import run_cli; sys.exit(run_cli(workspace=Path.cwd()))' \
  doctor --headless
```

`-I` prevents ambient `PYTHONPATH` or working-directory imports from selecting
another installation. Repo-owned Harbor children and credential-support assets
come from the imported runtime checkout, not `<workspace>/src` or
`<workspace>/containers`; task packages and policy remain workspace inputs.
This does not authorize new task versions, registration, experiments, or spend.

Before adopting the command for the two existing LaunchAgents, record the
runtime Git SHA, imported module path, interpreter/dependency state, original
plist bytes and loaded state, and all workspace/data roots. Wait for active
processes to settle. Retain labels, cadence, environment and log paths; replace
only the interpreter/entrypoint and disable tick's `RunAtLoad` for the reload so
bootstrap does not dispatch a queue tick. Do not kickstart either service or run
`tick`/`nightly` as a smoke. Observe the loaded argv and non-running state after
reload; a configured runtime is not evidence that a scheduled cycle succeeded.
The original schedule remains subject to its existing approval policy.

The release receipt records the exact before/after definitions and rollback:
unload the idle labels, restore their saved definitions, and reload without an
immediate `RunAtLoad` invocation. Retain the prior runtime checkout for rollback.

Queue events rotate before an append would take `queue/events.jsonl` past
10 MiB. Seven numbered archives are retained (`events.jsonl.1` is newest), and
all application readers scan the archives from oldest to newest before the
active file. A process lock serializes rotation and appends across launchd and
manual commands. The oldest archive is the only event file removed during a
rotation; size the retained window into any external backup or audit policy.

`tick` runs the headless doctor first. If any infrastructure check fails,
it appends a boolean-only quarantine event and dispatches nothing. In particular,
a locked or unreadable Keychain defers Claude specs; Codex and credentialless
controls remain independently eligible. A nonblocking process lock permits only
one executor to claim queue work; a concurrent tick records `executor_busy`
instead of double-dispatching or losing its terminal scheduler event.

## Nightly refresh

`evallab nightly --refresh` runs the installed local refresh.
It does not dispatch trials or start the researcher loop.
The command keeps its path options and backup freshness checks.
A missing or stale backup uses the existing 120-second timeout guard.
The former no-refresh dispatch pipeline and `--date` mode are retired.
Stored queues, receipts, reports, and backups remain unchanged.

## Executor resilience boundary

Every queue spec carries `timeout_seconds` (default 1,800; maximum 28,800). It
becomes Harbor's agent timeout (through `--agent-timeout-multiplier`).

The executor watches each active Harbor trial directory independently. It
terminates the Harbor process group when any trial exceeds its wall-clock
allowance, `timeout_seconds` + 600 s (`TRIAL_PHASE_ALLOWANCE_SECONDS`).
- **Why the 600 s:** it covers environment start, agent setup, the verifier
  and teardown. With it, an agent that runs to its own timeout is still
  verified.
- **Daytona:** the sandbox TTL uses the same bound.

The looser `(timeout_seconds + 600) * attempts` process deadline remains only a
pre-trial and discovery fail-safe. Timeout metadata names the triggering trial
when one was active. Docker discovery, inspection/removal, tool-version probes, and Git
metadata probes also have fixed subprocess timeouts; cleanup failure is recorded
as secondary evidence and cannot replace `trial_wall_clock_timeout` as the
primary reason.

Harbor writes a top-level `result.json` when a job starts, but that evidence is
not complete until `finished_at` is non-null. Restart reconciliation leaves a
partial job in `running/` and does not ingest or settle it. If the executor dies
after a terminal transient provider failure, or between archived retry phases,
the next tick moves the spec to `failed/` with a transient reason and preserves
all attempt evidence for explicit operator resubmission; it never calls the
provider implicitly during recovery. While any unresolved running spec remains,
the executor defers every approved spec with `running_specs_unresolved`; this
prevents detached work from overlapping new provider calls or escaping the
cost ceiling after a UTC day boundary.

After an interrupted Harbor run, cleanup snapshots only containers with all of
these properties: Docker Compose project labels, a Compose config path inside
`harbor/environments/docker`, a working directory under the current task, and a
Compose project matching a trial-session directory recorded in the current job.
The container ID must also have been absent before the run. Only those exact IDs
are passed to `docker rm -f`. The executor never runs a global container, image,
volume, or system prune, so the lab's PostgreSQL/Phoenix Compose project,
concurrent Harbor jobs, and unrelated containers are outside the cleanup set.

Structured provider-facing trial exceptions containing HTTP 429 and 5xx are
normalized to `transient_harness` with
the more specific queue reason `transient_harness:provider_http_429` or
`transient_harness:provider_http_5xx`. The executor permits at most two
infrastructure retries, with 5- then 10-second backoff (hard-capped at 30
seconds). Failed attempt evidence moves to
`runs/.transient-attempts/<job>/attempt-N/` before the same declared job is
retried. Successful jobs, task logs, and verifier failures are never classified
from free-form status-code text. Every billable attempt reserves `est_cost_usd`
in the locked, retained event ledger before launch. Failed-attempt reservations
survive executor restarts; the successful attempt is settled by catalog cost.
A later retry or spec is refused if catalog spend plus unsettled reservations
would cross the standing ceiling. Transient provider capacity
is shown separately in digests and skipped, rather than counted or treated as a
success, by the quiet-failure circuit breaker. An `AgentTimeoutError` or
`TrialBudgetExhaustedError` trial that the verifier scored ran end to end, so
the breaker treats it as a clean trial; without a verifier result either one
still counts as a failure.

Harbor subprocesses receive a non-secret environment allowlist. Model API-key
variables are neither accessed nor forwarded; supported model access remains
subscription authentication through Keychain or the agent's auth file. Codex's
non-secret force-auth-file switch is set by the executor. Claude runs through
`scripts/with-claude-auth`, which places only the Keychain OAuth token and
force-OAuth switches in the immediate Harbor child; it never aliases OAuth to
an API-key variable.

Every Harbor child, in-process Harbor call, launchd job, and wrapper script
runs with `HARBOR_TELEMETRY=off`. Harbor otherwise sends `harbor.job_finished` (job id,
agents, models, token usage, cost, duration and reward) to PostHog
([usage stats](https://docs.harborframework.com/telemetry/telemetry.md)). New Python
launch paths take their environment from `evallab.harbor_telemetry.harbor_child_environment`
or `execution_contracts.subscription_environment`.


The installed nightly command refreshes local reports.
It does not dispatch trials, ship traces, run researchers, or commit a digest.

```bash
uv run evallab nightly --refresh
```

Nightly refresh runs the Compose PostgreSQL
container's own `pg_dump` in custom format. Python does not read or forward a
database password: `POSTGRES_USER` and `POSTGRES_DB` expand inside the container.
The dump and SHA-256 JSON manifest are fsynced inside one dated generation
directory, then the complete directory is published with one atomic rename in
the primary checkout's ignored `backups/postgres/` directory. A same-date rerun
verifies and reuses the immutable generation rather than replacing it. A process
lock prevents overlapping manual and scheduled dumps.
A failed dump remains a backup failure in the refresh report.
Refresh does not dispatch trials or commit a digest.

See [third-party.md](third-party.md#backup-recovery) for PostgreSQL and SQLite backup and recovery procedures.

## Nightly digest and canary drift

The digest includes the prior day's trials, early-morning automation, policy
attribution, cost, exception taxonomy, queue depth, waiting rationales, evidence
growth, and quarantine state. PostgreSQL remains rebuildable from raw jobs; the
digest is a human-facing derived report.

The trial tables list evaluation runs. Self-test runs created by
`python -m evallab.smoke` (`smoke-<agent>-<token>` under `runs/_smoke/`) are
summarised rather than listed in full unless they raise an exception.

`tick --canary-suite` rejects a changed task digest or floating source ref before dispatch.
Digest excursions are labeled harness-drift suspects, never capability news.

## Run controls before model experiments

```bash
uv run evallab matrix research/experiments/local-controls.json
uv run evallab summarize runs
```

Expected outcome: Oracle has `reward=1`, no-op has `reward=0`, neither has an
exception, and both have verifier evidence. If either expectation fails, debug
the task or harness before spending model tokens.

## Ingest and query

```bash
uv run evallab ingest runs research/evidence/runs
```

Every completed job uses one idempotent ingest-and-project path. Queue completion and
`ingest` update
PostgreSQL first, then write a `jobs.parquet` marker per job and the
deterministic trial tables below the configured shared Parquet root at
`job_id=*/trial_id=*/`. The
job marker keeps zero-trial completed jobs inside the same invariant. Rebuilds
replace catalog inventories and Parquet partitions by stable Harbor UUID while
leaving raw evidence untouched.

Within a raw-data rebuild, each trial's normalized ATIF projection is reused
across trajectory, fact, and event-table exports. Job facts, including state
journals and artifact metadata, are built once and shared by the fact and event
exports rather than reconstructed for each. Normalized source data is released
after that job. Catalog ingestion likewise reuses its projection while indexing
that job's facts and documents. There is no process-global or cross-invocation
cache: subsequent calls reopen source data. This reduces repeated parsing and
hashing without changing table schemas, source digests, or the
catalog-before-Parquet failure boundary.

PostgreSQL is shared across linked Git worktrees, so Parquet must be shared too.
By default, every worktree resolves `derived/parquet` against the repository's
primary checkout, not against the invoking worktree. `.env.example` records the
equivalent explicit setting:

```dotenv
EVALLAB_DERIVED_ROOT=derived/parquet
```

A relative value is resolved against the primary checkout; an absolute value
may instead select a shared volume. The `ingest --derived-dir` flag is a deliberate
one-command override and remains relative to the invoking checkout.
This setting is storage topology, not authentication: model access remains
subscription-only through Keychain or the agent's auth file, and API-key
variables do not belong in this lab's `.env`.

To migrate an older worktree-local store, stop dispatch, copy each complete
`job_id=<uuid>` directory into the configured shared root without overwriting an
existing UUID, and run `uv run evallab doctor`. If the catalog contains a job
whose raw evidence was intentionally discarded, remove only that exact derived
catalog row; never drop or recreate the shared database to repair one stale job.
Once doctor reports equal catalog and projected counts, reinstall the schedule
and resume dispatch.

Because that store is shared across worktrees, doctor's `catalog-parquet` line
ends with `db=<host>:<port>/<dbname>` — the database it actually inspected,
never a credential. Two shells with different `DATABASE_URL` values otherwise
print the same green line for different catalogs.

Sharing is kept, but it is never silent (F-13). When a command run inside a
linked worktree inherits another checkout's derived root, `evallab` writes one
line to stderr naming the owner before doing anything with it:

```
evallab: derived root /Users/you/Developer/eval-lab/derived/parquet belongs to
/Users/you/Developer/eval-lab, not to this checkout
/Users/you/Developer/eval-lab/.worktrees/role, set EVALLAB_DERIVED_ROOT to an
absolute path to choose another.
```

The notice appears once per invoking tree and resolved root, so an interactive
command says it and the nightly loop does not repeat it. It is emitted only for
a resolution nobody named: a `--derived-dir` argument and an absolute
`EVALLAB_DERIVED_ROOT` are deliberate choices and stay quiet.
Concurrent catalog ingestion retries only PostgreSQL `DeadlockDetected` for
schema initialization, the base job/trial/reward/artifact phase, and the
document/fact phase. Each phase has at most four attempts, with 0.05s, 0.1s,
and 0.2s backoff (0.35s total sleep, not a transaction-duration limit).
Every attempt uses a fresh connection: the failed transaction rolls back
and closes before the complete phase replays, including commit-time failures.
One-shot job iterables are snapshotted once, preserving the full batch and
exact successful count. Exhausted deadlocks and every other error propagate,
so queue completion remains fail-closed. These are separate transactions;
already-committed phases remain committed if a later phase fails. Parquet
still writes only after all catalog phases finish.

A Parquet failure cannot roll back catalog ingest or turn a completed agent run
into an execution failure. It appends a
`projection_failed:<job-id>:<error-type>` queue event and leaves the job done so
the cause is visibly attributed to the harness. `uv run evallab doctor` enforces
the operational invariant: every catalog job has complete trial partitions, or
its exact job ID has such a recorded exception. To prove rebuildability, point
`EVALLAB_DERIVED_ROOT` at an empty isolated directory and run `uv run evallab ingest runs ...`.
Identical table row counts are expected.

## Evidence promotion checklist

Only small representative runs belong in `research/evidence/runs/`:

1. Confirm the run has final job and trial `result.json` files.
2. Inspect agent/verifier logs and all declared artifacts.
3. Check that no token, `.env`, prompt secret, credential file, or unrelated
   user data is present.
4. Confirm each file is small enough for ordinary Git review.
5. Copy the complete small job directory and run `uv run evallab summarize`.
6. Run the repository secret audit in `uv run pytest tests/test_repository_contract.py` before commit.

For large evidence, leave the job in ignored `runs/`, ingest its metadata, and
write a small report with digests and a durable artifact URI once object storage
is configured.
## Local checks

Run `make prepush` before each push. This checkpoint does not prove CI green.
It runs locked sync, Ruff, governance, registry audit, and the pinned typechecker.
Both focused forms leave the lessons lineage check to full static checks and CI.

- `make prepush` checks collection across the full test tree, then runs the automatic branch selection.
  Selection includes committed branch changes, local edits, deleted paths, both rename sides, and repository contracts.
  It selects direct module and asset consumers, not all transitive or dynamic dependencies.
  Runtime Python assets select their loader consumers. Nested fixture changes select descendant tests.
  Both devloop modes discover individually declared test files. Read errors stop selection instead of omitting consumers.
  Inspect it with `uv run evallab registry devloop --since origin/main --checkpoint --json`.
- `make prepush TESTS='tests/test_devloop.py [pytest args]'` replaces automatic test selection with explicit selectors.
  It does not run the automatic collection check or repository contracts unless you select them.
  It keeps the same focused static gates and uses a single pytest process.
- `make check` runs all static gates, including lessons. It runs no tests or runtime smoke.
- `make premerge` runs all static gates, the full Python 3.12 suite, and the Docker-free runtime smoke.
  Hosted CI still provides the separate native phase and supported Python matrix.

The typechecker uses a unique private file under `/tmp`, outside the checkout.
The script prints its complete output and removes that file on exit, including checker failure.
It does not write `runs/_premerge/ty.txt`. Existing receipts remain subject to the evidence retention policy.
Static, selection, collection, and test failures return a nonzero status.

## CI: pinned uv via PyPI, not marketplace action

Every workflow installs `uv==0.9.24` with `python3 -m pip install --user uv==0.9.24`. Every job that runs python or uv then provisions its interpreter with SHA-pinned `actions/setup-python` and exports `UV_PYTHON_PREFERENCE=only-system`, because the `cpython-3.12.12+20251217` linux x86_64 python-build-standalone asset serves 504 and a uv-managed download reddens the jobs that need 3.12 (HAR-330, `quality` run 42567901536; HAR-339, `perf` run 42582557769 and `mcp-wheelhouse-platform` run 42582557805). setup-python resolves the runner toolcache before any download, and `only-system` keeps uv on that interpreter — a missing pin fails at the provisioning step rather than uv downloading a managed build mid-run. No workflow installs an interpreter with uv. This replaces the former `astral-sh/setup-uv` step.

`scripts/premerge.sh` owns the uv version in `UV_VERSION`; workflow literals copy that pin.
Harbor 0.24.0 is a base dependency in `pyproject.toml`, independent of the optional Laminar exporter.
Run `uv sync --locked` to install its supported native SDK and console script.
The remaining isolated workflow CLI install selects `harbor==0.24.0`.
Update the runtime pin, lock, workflow install, and recorded native field contracts together.
The normal doctor treats a missing or drifting Harbor runtime as a failure, not an advisory.

Rationale (preserves the two invariants that existed before the outage):

- Exact version pin: `uv==0.9.24` is passed verbatim to pip; no "latest" or floating installer.
- Supply-chain posture: PyPI (already trusted by `uv sync --locked` and the repo's own lockfile) rather than a new codeload.github.com dependency. No new trust root, no unpinned `curl | sh`, no additional marketplace action.

`uv` lands in `$HOME/.local/bin` (added to `$GITHUB_PATH`). `actions/setup-python` provisions 3.12 for every workflow, and in `ci.yml` the test matrix provisions 3.12 and 3.14 plus 3.12 for `tools/mimoagent-harbor`; `uv sync`/`uv run`/`uv tool install` honour `UV_PYTHON` against those system interpreters. No cache step was added; correctness over convenience. No other job steps or commands were altered.

This change was made because a single throttled codeload path made every job unrunnable even when the rest of the pipeline was healthy. The PyPI path is a different CDN and has never exhibited the same 429/502/503 cascade in our runs. The PR that introduced it will itself run the modified workflows; a green result on that PR is the verification that the jobs remain executable.

The workflows moved back to `setup-python` (v6 SHA) because a single release asset made every 3.12 job unrunnable even while the sibling 3.14 asset served normally. The runner image already ships the provisioned versions in its toolcache, so the common path performs no network download at all. HAR-330 repaired `ci.yml` and `typecheck.yml`; HAR-339 moved the ten auxiliary workflows onto the same pin and aligned the two remaining v5 uses (the now-archived `loca-lean.yml` and `tau-knowledge.yml`).

## CI: uv package cache and bounded download retries (HAR-252)

`mcp-wheelhouse-platform` run 41387496649 failed on `main` on 2026-11-02 with
`Failed to download pytz==2026.5 after 3 retries`. uv stops after its default three
HTTP retries, and only `ci.yml` and `typecheck.yml` restored a package cache, so the
other ten jobs re-downloaded the lock on every run. That is one such failure in the
last 300 `main` runs, and it failed a required workflow.

Every workflow job that resolves packages now:

- restores `~/.cache/uv` through `actions/cache` (the `v6.1.0` SHA that `ci.yml`
  already pins) before its first sync, keyed by
  `hashFiles('uv.lock', '.github/workflows/<file>')` with `restore-keys` falling back
  to the `ci.yml` test cache, and `SEGMENT_DOWNLOAD_TIMEOUT_MINS: 2` so a slow restore
  yields to a plain download; and
- sets `UV_HTTP_RETRIES: "5"` in the workflow `env`, above uv's default of 3.
  `UV_HTTP_RETRIES` exists since uv 0.7.21 and the workflows pin uv 0.9.24: a local
  index answering 503 failed immediately with `UV_HTTP_RETRIES=0`, reported
  `Request failed after 3 retries` at 3 and `after 5 retries` at 5.

`uv run` without `--no-sync` syncs implicitly, so those jobs carry the same cover. Jobs
that only run `uv tool install`, `uv python install` or `uvx` need the retry floor but
no project cache.

Cache trust: entries hold only wheels and index responses, the artifacts
`uv sync --locked` already verifies against the lock hashes. A cache saved by a pull
request is scoped to that pull request, so `main` restores only entries created on
`main` or by its own runs. This adds no new third-party trust root beyond the
SHA-pinned `actions/cache` the quality and typecheck jobs already used.

The core lint and typecheck fallback keys include `tools/mimoagent-harbor/uv.lock`.
They must match the test cache writer's complete digest.
The jobs keep separate writer keys and rerun every check after a cache hit.

## CI: native test scheduling

The native inventory uses 2 pytest workers with `--dist load --maxschedchunk 2`.
This distributes individual tests, including the isolated native worker cases.
Without the chunk limit, xdist sends about 26 consecutive tests to 1 worker, so the 32 slow worker cases ran on 1 process.
In each shard log, compare the native pytest wall time with the `Module durations` total: a ratio near 1 means the 2 workers did not overlap.
The default `loadscope` scheduler keeps that module on 1 worker, even with more workers.
The base suite keeps its existing scheduler.
Each native shard retains its full file inventory, JUnit report, and Harbor skip census.
The interpreter matrix, dependency profiles, pinned source preparation, and required gates do not change.

## CI: one dependency install and direct schema setup

Each test job installs benchmarks, inspect, and the Laminar extra in 1 locked root sync.
The native phase reuses that environment. It keeps the separate Python 3.12 worker sync.
The default `dev` group already includes Laminar, so the former second root sync added no dependency.
It only removed inspect packages before the native session.

The fresh PostgreSQL service receives `sql/schema.sql` through the runner's `psql`.
`-X` ignores user startup files. `--single-transaction` preserves atomic schema application.
`--set ON_ERROR_STOP=1` fails the setup step on any SQL error.
This avoids importing the application CLI before the tests.
The database service, both pytest sessions, both JUnit files, and both skip censuses remain unchanged.


## Agent profiles and model routes

`profiles.py` owns immutable adapter/model identity and qualification evidence.
There is no dedicated `evallab profiles` command.
Controls have no model credential. Billable profiles require a pinned model and a declared secret source.
Secret sources contain identifiers, never token values.
The subscription environment uses an allowlist and removes key-shaped names.

Qualification states remain separate:
`declared → installed → credential-ready → smoke-passed → canary-qualified`.
A declared model name or readable credential does not establish a successful run.
Report the dated proving trial, exact model, CLI version, and harness.
Old profile tables do not establish present availability.

Profile preflight fails closed without a wired probe or with failed/expired identity evidence.
A preflight refusal occurs before a trial exists. It is not reward 0.
Credential failures are operational failures, not capability measurements.
Tests inject the home, clock, environment, credential probe, and subprocess runner.

Use the suitable subscription route before a paid API route.
A rate limit requires waiting for its reset, not changing the same job to an API.
Pin the native account model, adapter, and CLI version for each experiment.
Check `harbor_codex.PINNED_CODEX_VERSION`, the profile registry, and the native runtime contract.
The retained `gepa_optimizer/codex_transport.py` supports native QA callers.
It does not retain a GEPA optimization command.

Claude gateway execution uses the host bridge in `containers/claude_gateway_bridge.py`.
The sandbox receives a per-trial capability, not a provider credential.
The bridge checks returned model identity and records bounded usage.
The Keychain and gateway paths have different qualification evidence.
Do not treat a historical success on one route as qualification of the other.
The full adapter and backend rules remain in [execution tiers](execution-tiers.md).

## Quota measurement and portable sidecars

`Headroom` reports the provider's account-wide allowance.
`ConsumptionLedger` reports the lab's recorded trials and usage.
The provider counter cannot attribute a percentage to the lab.
No counter movement below its measurement resolution proves zero consumption.
`paid_trials` and `trials_with_observed_usage` have different denominators.
Missing usage stays unavailable.

Codex headroom reads retained `payload.rate_limits`, not transcript text or credentials.
When a trial has no rollout snapshots, the reader accepts its retained
`agent/quota/*.rate-limits.json` sidecars.
This is a fallback, never an addition to the same rollout history.
The reading uses the source observation time, never the sidecar modification time.
The sidecar whitelist excludes prompts, reasoning, account identifiers, and credentials.
Its source hash identifies omitted raw evidence without recreating that content.
A committed reading is a dated snapshot, not a current balance.

Claude dispatch reads `omp usage -p anthropic --json -r` through a bounded subprocess.
This is a usage lookup, not inference.
The reader retains only the exact `anthropic:5h` and `anthropic:7d` windows.
Both windows and their fetch time must validate.
Missing, malformed, timed-out, or expired readings pause Claude dispatch.
The historical Codex bootstrap rule does not apply to that refreshable live reader.

Claude dispatch records the sanitized observation in `claude-dispatch-quota.json`,
`lab-metadata.json`, and each trial's `agent/quota/claude-dispatch.json`.
Each retry gets a fresh reading for that attempt.
The record proves what admission observed, not what the lab consumed.
`evallab preflight` remains a disk-only reporting surface.

## Retained LoRA training and serving

The SFT producer commands are retired. Retained exports and isolated training tools remain.
The HAR-129 source evidence lives in `research/experiments/har129-lora/`.
It proves that dated study, not present authorization or current cost.

Training accepts an existing `evallab.sft_terminus/1` export with
`conversations.jsonl`, `manifest.json`, and its conversation digest.
Run the offline mask audit before any approved training:

```sh
uv run --project tools/modal-mimo-sft --locked python tools/modal-mimo-sft/sft.py dry-run \
  --data <export> --epochs 1
```

Report sequence tokens, trained tokens, longest row, and manifest digest.
Rows above the training limit fail by label. Do not truncate them.
The training limit and served context limit are different contracts.
For last-turn loss, history matches the model-visible messages; only the target carries its reasoning.
The rendered target and mask must match the native template.

Paid training needs separate current approval before `train --confirm-spend`.
The trainer audits the exact prepared row order, input IDs, and labels before training.
Retain adapter file hashes, data hashes, loss history, memory, duration, and audit counts.
Serving uses the isolated `tools/modal-mimo-serve` project and a declared LoRA binding.
Only admitted adapter names can appear in a queue selector.
An admitted name does not create or deploy an adapter.
Require its training receipt, serving path, and native returned-model identity.
A base-model echo cannot satisfy an adapter request.

Before paired evaluation, require byte-identical base output at temperature 0,
valid Terminus turns, and an adapter-application proof.
Equal greedy text alone cannot show whether an adapter applies.
Retain the base/base, adapter/adapter, and base/adapter probe evidence.
Training, serving, parity requests, and teardown remain owner-controlled paid operations.
Do not deploy or stop shared apps as a repository smoke.

## Retained rollout accounting

The `evallab rollout` frontend is retired.
Its retained result files remain historical evidence with the following accounting limits.

`results.jsonl` publishes each terminal task/attempt atomically.
Non-done queue states retain an error and null reward/pass.
After clean settlement, native verifier reward defines the benchmark result.
Native agent exceptions remain visible and can independently prevent SFT admission.
Missing verifier reward is an unmeasured result, not zero.

`summary.json` counts only measured completions in throughput and pass-rate denominators.
Costs include failed and unmeasured persisted outcomes.
Any missing model-cost or sandbox-duration evidence leaves the corresponding total null.
After driver death, compare persisted results with the authorized spec list.
Missing specs remain unmeasured.

Model usage comes from the strict proxy lifecycle ledger.
Settled tokens exclude unresolved reservations.
Settled dollars require a complete recorded price table; a cumulative dollar number alone is insufficient.
Accounting errors do not replace a native grade or dispatcher error.
Sandbox cost remains an estimate. A zero token price does not price a self-hosted serving app.
Provider invoices, unresolved exposure, and serving-session costs remain separate.

## Scaling and work orders

Use current measured queue, storage, and query limits before selecting new infrastructure.
Object storage requires durable transfer/recovery needs. Remote execution requires an approved runtime and cost envelope.
A workflow engine or analytical database requires a measured bottleneck that the existing queue or DuckDB cannot meet.
Old builder work orders, model price tables, and operating demos remain in Git history.
They are not live assignments, current prices, or permission to restore retired products.
